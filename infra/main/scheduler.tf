resource "aws_scheduler_schedule_group" "pipeline" {
  name = "${var.resource_prefix}-schedules"
}

resource "aws_scheduler_schedule" "daily" {
  name       = "${var.resource_prefix}-daily"
  state      = "ENABLED"
  group_name = aws_scheduler_schedule_group.pipeline.name

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = var.schedule_timezone

  target {
    arn      = aws_lambda_function.scheduler_entrypoint.arn
    role_arn = aws_iam_role.scheduler.arn

    retry_policy {
      maximum_event_age_in_seconds = 3600
      maximum_retry_attempts       = 0
    }
  }
}

resource "aws_scheduler_schedule" "runner_janitor" {
  name       = "${var.resource_prefix}-runner-janitor-hourly"
  state      = "ENABLED"
  group_name = aws_scheduler_schedule_group.pipeline.name

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = "rate(1 hour)"
  schedule_expression_timezone = "UTC"

  target {
    arn      = aws_lambda_function.runner_janitor.arn
    role_arn = aws_iam_role.scheduler.arn

    retry_policy {
      maximum_event_age_in_seconds = 3600
      maximum_retry_attempts       = 0
    }
  }
}
