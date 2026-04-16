locals {
  custom_domain_requested = trimspace(var.public_hostname) != ""
  custom_domain_enabled   = local.custom_domain_requested && var.enable_custom_domain
}

resource "aws_acm_certificate" "live" {
  count             = local.custom_domain_requested ? 1 : 0
  domain_name       = var.public_hostname
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_acm_certificate_validation" "live" {
  count                   = local.custom_domain_enabled ? 1 : 0
  certificate_arn         = aws_acm_certificate.live[0].arn
  validation_record_fqdns = [for option in aws_acm_certificate.live[0].domain_validation_options : option.resource_record_name]
}
