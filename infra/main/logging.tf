resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.resource_prefix}-scheduler-entrypoint"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "janitor_lambda" {
  name              = "/aws/lambda/${var.resource_prefix}-runner-janitor"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "step_functions" {
  name              = "/aws/vendedlogs/states/${var.resource_prefix}-pipeline"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "ssm_runner" {
  name              = "/aws/ssm/${var.resource_prefix}-runner"
  retention_in_days = var.log_retention_days
}
