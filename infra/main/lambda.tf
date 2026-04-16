data "archive_file" "scheduler_entrypoint" {
  type        = "zip"
  source_file = "${path.module}/lambda/scheduler_entrypoint.py"
  output_path = "${path.module}/build/scheduler_entrypoint.zip"
}

data "archive_file" "runner_janitor" {
  type        = "zip"
  source_file = "${path.module}/lambda/runner_janitor.py"
  output_path = "${path.module}/build/runner_janitor.zip"
}

resource "aws_lambda_function" "scheduler_entrypoint" {
  function_name    = "${var.resource_prefix}-scheduler-entrypoint"
  filename         = data.archive_file.scheduler_entrypoint.output_path
  source_code_hash = data.archive_file.scheduler_entrypoint.output_base64sha256
  role             = aws_iam_role.scheduler_entrypoint.arn
  handler          = "scheduler_entrypoint.handler"
  runtime          = "python3.13"
  timeout          = 60

  environment {
    variables = {
      ARTIFACTS_BUCKET    = aws_s3_bucket.artifacts.bucket
      LANGUAGE            = var.language
      NOTIFICATIONS_ARN   = aws_sns_topic.notifications.arn
      STATE_MACHINE_ARN   = aws_sfn_state_machine.pipeline.arn
      WIKIMEDIA_DUMPS_URL = "https://dumps.wikimedia.org"
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

resource "aws_lambda_function" "runner_janitor" {
  function_name    = "${var.resource_prefix}-runner-janitor"
  filename         = data.archive_file.runner_janitor.output_path
  source_code_hash = data.archive_file.runner_janitor.output_base64sha256
  role             = aws_iam_role.runner_janitor.arn
  handler          = "runner_janitor.handler"
  runtime          = "python3.13"
  timeout          = 60

  environment {
    variables = {
      ENVIRONMENT           = var.environment
      GRACE_SECONDS         = tostring(var.runner_janitor_grace_seconds)
      NOTIFICATIONS_ARN     = aws_sns_topic.notifications.arn
      PROJECT               = var.project
      RESOURCE_PREFIX       = var.resource_prefix
      STATE_MACHINE_TIMEOUT = tostring(var.state_machine_timeout_seconds)
    }
  }

  depends_on = [aws_cloudwatch_log_group.janitor_lambda]
}

resource "aws_lambda_permission" "scheduler_invoke" {
  statement_id  = "AllowEventBridgeSchedulerInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scheduler_entrypoint.function_name
  principal     = "scheduler.amazonaws.com"
}

resource "aws_lambda_permission" "runner_janitor_invoke" {
  statement_id  = "AllowEventBridgeSchedulerInvokeJanitor"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.runner_janitor.function_name
  principal     = "scheduler.amazonaws.com"
}
