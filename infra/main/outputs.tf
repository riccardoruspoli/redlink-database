output "artifacts_bucket_name" {
  value = aws_s3_bucket.artifacts.bucket
}

output "live_bucket_name" {
  value = aws_s3_bucket.live.bucket
}

output "cloudfront_domain_name" {
  value = aws_cloudfront_distribution.live.domain_name
}

output "public_hostname" {
  value = local.custom_domain_requested ? var.public_hostname : null
}

output "acm_certificate_arn" {
  value = local.custom_domain_requested ? aws_acm_certificate.live[0].arn : null
}

output "acm_certificate_validation_records" {
  value = local.custom_domain_requested ? [
    for option in aws_acm_certificate.live[0].domain_validation_options : {
      name  = option.resource_record_name
      type  = option.resource_record_type
      value = option.resource_record_value
    }
  ] : []
}

output "state_machine_arn" {
  value = aws_sfn_state_machine.pipeline.arn
}

output "lambda_function_name" {
  value = aws_lambda_function.scheduler_entrypoint.function_name
}
