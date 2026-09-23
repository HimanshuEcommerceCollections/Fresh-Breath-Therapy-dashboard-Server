# Deployment pipeline — one-time AWS setup

Everything in this branch is inert until the AWS side exists. This document is
the other half: what has to be created once, by hand or in Terraform, before
`.github/workflows/deploy.yml` can run.

```
  push to master
        │
        ▼
  ┌─────────────────────────────────────────────────┐
  │ build-and-test                                  │
  │   build image (once)                            │
  │   assert: not root, no .env/venv/qa in image    │
  │   throwaway Postgres → alembic upgrade head     │
  │   boot container → /health                      │
  │   assert: /docs /redoc /openapi.json are 404    │
  │   assert: HSTS, nosniff, X-Request-ID, no-store │
  │   push the tested image to ECR (sha tag)        │
  └─────────────────────────────────────────────────┘
        │
        ▼
  ┌─────────────────────────────────────────────────┐
  │ deploy                                          │
  │   register one task definition revision         │
  │   run-task: alembic upgrade head → wait → exit? │
  │   update-service → wait for stable              │
  └─────────────────────────────────────────────────┘
```

Pull requests run the top box and stop. They never reach AWS.

---

## 1. Replace the placeholders

`ecs/task-definition.json` ships with placeholders. All of them must be
substituted before the first deploy:

| Placeholder | What it is |
|---|---|
| `<AWS_ACCOUNT_ID>` | 12-digit account number |
| `<AWS_REGION>` | e.g. `us-east-1` |
| `<PRODUCTION_FRONTEND_HOST>` | where the dashboard is served |
| `<PRODUCTION_API_HOST>` | the ALB/CloudFront hostname for this API |
| `secret:fbt/backend-XXXXXX` | the real Secrets Manager ARN, suffix included |

The `image` value is a placeholder too, but that one is replaced automatically
on every run — leave it.

## 2. GitHub repository variables

Settings → Secrets and variables → Actions → **Variables** (not Secrets: none of
these are sensitive, and a variable is visible in logs, which helps when a
deploy goes wrong).

| Variable | Example |
|---|---|
| `AWS_REGION` | `us-east-1` |
| `AWS_DEPLOY_ROLE_ARN` | `arn:aws:iam::123456789012:role/fbt-github-deploy` |
| `ECS_SUBNETS` | `subnet-0aaa,subnet-0bbb` (private subnets, comma-separated, no spaces) |
| `ECS_SECURITY_GROUP` | `sg-0ccc` |

There are deliberately **no AWS secrets here.** See §4.

## 3. ECR repository

```bash
aws ecr create-repository \
  --repository-name fbt-backend \
  --image-tag-mutability IMMUTABLE \
  --image-scanning-configuration scanOnPush=true \
  --encryption-configuration encryptionType=KMS
```

`IMMUTABLE` is why the pipeline pushes only a commit-sha tag and never `latest`.
A mutable tag means the image a task definition points at can be swapped after
the fact, which quietly destroys the link between a running container and the
commit that produced it — exactly the link an incident investigation needs.

Add a lifecycle policy so untagged and old images do not accumulate:

```json
{
  "rules": [{
    "rulePriority": 1,
    "description": "Keep the last 30 images",
    "selection": { "tagStatus": "any", "countType": "imageCountMoreThan", "countNumber": 30 },
    "action": { "type": "expire" }
  }]
}
```

## 4. GitHub → AWS without a permanent key

The usual approach is an IAM user with an access key stored in GitHub secrets.
That is a permanent credential to the production account, held on a third
party's servers, that nothing ever rotates and no audit trail covers. OIDC
replaces it: GitHub presents a short-lived signed token, AWS verifies it and
issues credentials that expire in an hour.

**Identity provider** (once per account):

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

**Trust policy** for `fbt-github-deploy` — note the `sub` condition pins it to
this repository *and this branch*. A pull request, a fork, or a push to any
other branch cannot assume this role even if someone edits the workflow file:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Federated": "arn:aws:iam::<AWS_ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com" },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:HimanshuEcommerceCollections/Fresh-Breath-Therapy-dashboard-Server:ref:refs/heads/master"
      }
    }
  }]
}
```

**Permissions policy** — what the pipeline does and nothing else:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EcrLogin",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    },
    {
      "Sid": "EcrPush",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer"
      ],
      "Resource": "arn:aws:ecr:<AWS_REGION>:<AWS_ACCOUNT_ID>:repository/fbt-backend"
    },
    {
      "Sid": "RegisterTaskDefinitions",
      "Effect": "Allow",
      "Action": ["ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition"],
      "Resource": "*"
    },
    {
      "Sid": "DeployAndMigrate",
      "Effect": "Allow",
      "Action": ["ecs:RunTask", "ecs:DescribeTasks", "ecs:UpdateService", "ecs:DescribeServices"],
      "Resource": [
        "arn:aws:ecs:<AWS_REGION>:<AWS_ACCOUNT_ID>:service/fbt-production/fbt-backend",
        "arn:aws:ecs:<AWS_REGION>:<AWS_ACCOUNT_ID>:task-definition/fbt-backend:*",
        "arn:aws:ecs:<AWS_REGION>:<AWS_ACCOUNT_ID>:task/fbt-production/*"
      ]
    },
    {
      "Sid": "PassOnlyTheseRoles",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": [
        "arn:aws:iam::<AWS_ACCOUNT_ID>:role/fbt-ecs-task-execution",
        "arn:aws:iam::<AWS_ACCOUNT_ID>:role/fbt-backend-task"
      ],
      "Condition": { "StringEquals": { "iam:PassedToService": "ecs-tasks.amazonaws.com" } }
    },
    {
      "Sid": "ReadMigrationLogs",
      "Effect": "Allow",
      "Action": ["logs:GetLogEvents"],
      "Resource": "arn:aws:logs:<AWS_REGION>:<AWS_ACCOUNT_ID>:log-group:/ecs/fbt-backend:*"
    }
  ]
}
```

`ecs:RegisterTaskDefinition` cannot be scoped to a family — AWS does not support
it. The `iam:PassRole` restriction is what stops that being dangerous: without
it, permission to register any task definition is permission to run a container
as *any* role in the account.

## 5. The two task roles

They are different things and conflating them is a common mistake.

**`fbt-ecs-task-execution`** — used by the ECS agent *before* the container
starts, to pull the image and fetch secrets. Trust `ecs-tasks.amazonaws.com`.
Attach `AmazonECSTaskExecutionRolePolicy`, plus:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "secretsmanager:GetSecretValue",
    "Resource": "arn:aws:secretsmanager:<AWS_REGION>:<AWS_ACCOUNT_ID>:secret:fbt/backend-*"
  }, {
    "Effect": "Allow",
    "Action": "kms:Decrypt",
    "Resource": "arn:aws:kms:<AWS_REGION>:<AWS_ACCOUNT_ID>:key/<SECRETS_KMS_KEY_ID>"
  }]
}
```

**`fbt-backend-task`** — assumed by the application itself, at runtime. It needs
nothing today. When the storage seam is switched to S3 and email to SES, the
S3 and `ses:SendRawEmail` permissions go *here*, not on the execution role.

## 6. Secrets Manager

One secret, `fbt/backend`, holding a JSON object. The task definition pulls
individual keys out of it with the `:KEY::` suffix syntax, so there is one
secret to rotate rather than nine.

```json
{
  "DATABASE_URL": "postgresql+asyncpg://fbt_app:...@fbt.xxxx.<region>.rds.amazonaws.com:5432/fbt?ssl=require",
  "MIGRATION_DATABASE_URL": "postgresql+asyncpg://fbt_migrator:...@fbt.xxxx.<region>.rds.amazonaws.com:5432/fbt?ssl=require",
  "SECRET_KEY": "<64 random bytes, base64>",
  "GOOGLE_CLIENT_ID": "...",
  "GOOGLE_CLIENT_SECRET": "...",
  "SMTP_USER": "<SES SMTP username>",
  "SMTP_PASSWORD": "<SES SMTP password>",
  "LEAD_WEBHOOK_SECRET": "...",
  "CRON_SECRET": "..."
}
```

Encrypt with a customer-managed KMS key, not the AWS-managed default — a CMK is
the only version whose key policy you control and whose use appears in
CloudTrail as a distinct event.

`MIGRATION_DATABASE_URL` is separate on purpose. It is the seam for giving the
migration task a role with DDL rights while the application's own role has none,
which is the same shape as the audit-log purge credential (item A2). Point both
at the same user for the first deploy if that is simpler; splitting them later
is a secret edit, not a code change.

## 7. Log group

```bash
aws logs create-log-group --log-group-name /ecs/fbt-backend --kms-key-id <arn>
aws logs put-retention-policy --log-group-name /ecs/fbt-backend --retention-in-days 365
```

Create it explicitly — the task definition sets `awslogs-create-group: false`,
so a missing group fails the task rather than silently creating an unencrypted,
never-expiring one.

Encryption matters more here than it looks. `app/services/log_redaction.py`
strips emails and phone numbers from log lines, but it cannot catch a name, and
a stack trace can carry one. Treat this log group as capable of holding PHI.

## 8. The service

```bash
aws ecs create-service \
  --cluster fbt-production \
  --service-name fbt-backend \
  --task-definition fbt-backend \
  --desired-count 1 \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[<private-subnets>],securityGroups=[<sg>],assignPublicIp=DISABLED}" \
  --load-balancers "targetGroupArn=<tg-arn>,containerName=fbt-backend,containerPort=8000" \
  --health-check-grace-period-seconds 60 \
  --deployment-configuration "deploymentCircuitBreaker={enable=true,rollback=true},minimumHealthyPercent=100,maximumPercent=200"
```

The circuit breaker is what makes a bad deploy self-correcting: if the new tasks
never pass their health check, ECS reverts to the previous revision on its own,
and `aws ecs wait services-stable` in the workflow then fails, so the pipeline
goes red instead of reporting a success over a rollback.

`assignPublicIp=DISABLED` with private subnets means the tasks need a NAT
gateway or VPC endpoints to reach ECR, Secrets Manager and CloudWatch. Interface
endpoints are the cheaper and tighter option.

**Security groups:** the task's group should accept ingress on 8000 *from the
ALB's security group only* — not from a CIDR. The Dockerfile passes
`--forwarded-allow-ips='*'` to uvicorn, which means it trusts the
`X-Forwarded-For` of whatever connects to it. That is correct only while nothing
but the ALB can connect. If that port is ever reachable more widely, a client
can forge its own source address, and every audit row and every rate-limit
decision starts believing it.

---

## ⚠️ Read this before scaling past one task

`app/main.py` starts an **in-process APScheduler** (`start_scheduler()`), which
runs three jobs: the notification sweep every 15 minutes, the audit-log purge
daily, and the data-retention sweep daily.

Every task runs its own copy. Two tasks means two notification sweeps, so users
get duplicate reminders, and two concurrent retention sweeps racing each other.

So, until the scheduler moves out of the web process:

- `desired-count` stays at **1**
- `WEB_CONCURRENCY` stays at **1** (uvicorn workers are separate processes too)

That caps this API at one task, which is a real availability limit — during a
deploy there is a brief window with no healthy task unless
`minimumHealthyPercent=100` is honoured, and there is no redundancy if an AZ
fails.

The fix is to pull the three jobs out into EventBridge Scheduler rules that
invoke a one-off ECS task (the same image, command overridden, exactly how
migrations run in this pipeline). `/api/internal/notification-scan` already
exists and is guarded by `CRON_SECRET`, so the notification sweep can move
first with no code change at all. That work is not in this branch.

## ⚠️ `readonlyRootFilesystem` is set to `true`

Nothing in `app/` writes to disk — checked — and `PYTHONDONTWRITEBYTECODE=1`
stops the interpreter doing it. `/tmp` is mounted writable from an ephemeral
volume for anything in a library that assumes it can.

It is still the setting most likely to surprise on the first deploy. If a task
crashes with a read-only filesystem error, flip it to `false`, deploy, and open
an issue rather than leaving it off silently.

## Migrations and rolling deploys

Migrations run *before* the new tasks start, and during the rollout old and new
tasks serve traffic simultaneously against the already-migrated schema. So every
migration must be backwards-compatible with the revision it is replacing:

- **Safe in one deploy:** adding a nullable column, adding a table, adding an
  index (use `CONCURRENTLY` on a large table), widening a type.
- **Needs two deploys:** dropping a column, renaming one, adding a `NOT NULL`
  without a default, tightening a constraint. Deploy the code that stops using
  it first; drop it in the deploy after.

`alembic upgrade head` is not transactional across revisions. A chain that fails
on revision 4 of 6 leaves the first three applied, and the workflow stops before
touching the service — so the old code is still running against a partially
migrated schema. That is the safer of the two failure modes, but it does mean a
failed migration needs a human, not a retry.

## What is deliberately not here

- **Frontend.** The Next.js dashboard is not in this repository and this
  pipeline does not touch it. Its AWS story is CloudFront and S3, or Amplify,
  and it is a separate decision.
- **Terraform.** These are `aws` CLI commands so they can be read and run one at
  a time during a cutover. Anything that survives should end up in Terraform.
- **Base image digest pinning.** `python:3.12.7-slim-bookworm` is a tag, and a
  tag can be re-pushed. Pin the digest (`@sha256:...`) once the first build has
  resolved one, and let Dependabot raise it.
- **The dependency audit is advisory.** `dependency-audit` runs
  `continue-on-error: true` because two known, assessed advisories are open
  (`ecdsa`, no fixed release and not on the HS256 path; `pip`, build tooling
  absent from the runtime image). Record their IDs as `--ignore-vuln` flags with
  a dated note of who accepted them, then delete `continue-on-error` so a *new*
  advisory actually fails the build.
