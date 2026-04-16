locals {
  wiki_name             = "${var.language}wiki"
  artifacts_bucket_arn  = aws_s3_bucket.artifacts.arn
  live_bucket_arn       = aws_s3_bucket.live.arn
  wiki_artifacts_prefix = "${local.wiki_name}/*"
  ssm_document_arn      = "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}::document/AWS-RunShellScript"
  ec2_instance_arn_glob = "arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/*"
}

data "aws_iam_policy_document" "ec2_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      identifiers = ["ec2.amazonaws.com"]
      type        = "Service"
    }
  }
}

resource "aws_iam_role" "runner" {
  name               = "${var.resource_prefix}-runner-role"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json
}

resource "aws_iam_role_policy_attachment" "runner_ssm" {
  role       = aws_iam_role.runner.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "runner_cloudwatch_agent" {
  role       = aws_iam_role.runner.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

data "aws_iam_policy_document" "runner_s3" {
  statement {
    actions = [
      "s3:DeleteObject",
      "s3:GetObject",
      "s3:PutObject",
    ]

    resources = [
      "${local.artifacts_bucket_arn}/${local.wiki_artifacts_prefix}",
      "${local.live_bucket_arn}/*",
    ]
  }

  statement {
    actions = [
      "s3:GetBucketLocation",
      "s3:ListBucket",
    ]

    resources = [local.artifacts_bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values = [
        "${local.wiki_name}",
        "${local.wiki_name}/",
        local.wiki_artifacts_prefix,
      ]
    }
  }

  statement {
    actions = [
      "s3:GetBucketLocation",
      "s3:ListBucket",
    ]
    resources = [local.live_bucket_arn]
  }
}

resource "aws_iam_role_policy" "runner_s3" {
  name   = "${var.resource_prefix}-runner-s3"
  role   = aws_iam_role.runner.id
  policy = data.aws_iam_policy_document.runner_s3.json
}

resource "aws_iam_instance_profile" "runner" {
  name = "${var.resource_prefix}-runner-profile"
  role = aws_iam_role.runner.name
}

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      identifiers = ["lambda.amazonaws.com"]
      type        = "Service"
    }
  }
}

resource "aws_iam_role" "scheduler_entrypoint" {
  name               = "${var.resource_prefix}-scheduler-entrypoint-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "scheduler_entrypoint_basic_execution" {
  role       = aws_iam_role.scheduler_entrypoint.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "scheduler_entrypoint" {
  statement {
    actions = [
      "states:ListExecutions",
      "states:StartExecution",
    ]

    resources = [aws_sfn_state_machine.pipeline.arn]
  }

  statement {
    actions = [
      "sns:Publish",
    ]

    resources = [aws_sns_topic.notifications.arn]
  }

  statement {
    actions   = ["s3:ListBucket"]
    resources = [local.artifacts_bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values = [
        "${local.wiki_name}",
        "${local.wiki_name}/",
        local.wiki_artifacts_prefix,
      ]
    }
  }
}

resource "aws_iam_role_policy" "scheduler_entrypoint" {
  name   = "${var.resource_prefix}-scheduler-entrypoint"
  role   = aws_iam_role.scheduler_entrypoint.id
  policy = data.aws_iam_policy_document.scheduler_entrypoint.json
}

resource "aws_iam_role" "runner_janitor" {
  name               = "${var.resource_prefix}-runner-janitor-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "runner_janitor_basic_execution" {
  role       = aws_iam_role.runner_janitor.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "runner_janitor" {
  statement {
    actions   = ["ec2:DescribeInstances"]
    resources = ["*"]
  }

  statement {
    actions   = ["ec2:TerminateInstances"]
    resources = [local.ec2_instance_arn_glob]

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Project"
      values   = [var.project]
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Environment"
      values   = [var.environment]
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Role"
      values   = ["runner"]
    }
  }

  statement {
    actions = [
      "sns:Publish",
    ]
    resources = [aws_sns_topic.notifications.arn]
  }
}

resource "aws_iam_role_policy" "runner_janitor" {
  name   = "${var.resource_prefix}-runner-janitor"
  role   = aws_iam_role.runner_janitor.id
  policy = data.aws_iam_policy_document.runner_janitor.json
}

data "aws_iam_policy_document" "step_functions_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      identifiers = ["states.amazonaws.com"]
      type        = "Service"
    }
  }
}

resource "aws_iam_role" "step_functions" {
  name               = "${var.resource_prefix}-step-functions-role"
  assume_role_policy = data.aws_iam_policy_document.step_functions_assume_role.json
}

data "aws_iam_policy_document" "step_functions" {
  statement {
    actions = [
      "ec2:RunInstances",
      "ec2:DescribeInstances",
      "ec2:TerminateInstances",
      "ec2:CreateTags",
    ]
    resources = ["*"]
  }

  statement {
    actions = [
      "iam:PassRole",
    ]
    resources = [aws_iam_role.runner.arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
  }

  statement {
    actions = ["ssm:SendCommand"]
    resources = [
      local.ssm_document_arn,
      local.ec2_instance_arn_glob,
    ]
  }

  statement {
    actions   = ["ssm:GetCommandInvocation"]
    resources = ["*"]
  }

  statement {
    actions = [
      "sns:Publish",
    ]
    resources = [aws_sns_topic.notifications.arn]
  }

  statement {
    actions = [
      "logs:CreateLogDelivery",
      "logs:GetLogDelivery",
      "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies",
      "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "step_functions" {
  name   = "${var.resource_prefix}-step-functions"
  role   = aws_iam_role.step_functions.id
  policy = data.aws_iam_policy_document.step_functions.json
}

data "aws_iam_policy_document" "scheduler_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      identifiers = ["scheduler.amazonaws.com"]
      type        = "Service"
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.resource_prefix}-scheduler-role"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume_role.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    actions = ["lambda:InvokeFunction"]
    resources = [
      aws_lambda_function.scheduler_entrypoint.arn,
      aws_lambda_function.runner_janitor.arn,
    ]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "${var.resource_prefix}-scheduler"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}
