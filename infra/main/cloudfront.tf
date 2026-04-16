resource "aws_cloudfront_origin_access_control" "live" {
  name                              = "${var.resource_prefix}-live-oac"
  description                       = "Origin access control for the live site bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "live" {
  enabled             = true
  default_root_object = "index.html"
  price_class         = "PriceClass_100"
  wait_for_deployment = false
  aliases             = local.custom_domain_enabled ? [var.public_hostname] : []

  origin {
    domain_name              = aws_s3_bucket.live.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.live.id
    origin_id                = "live-s3-origin"
  }

  default_cache_behavior {
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    target_origin_id       = "live-s3-origin"
    viewer_protocol_policy = "redirect-to-https"

    forwarded_values {
      query_string = false

      cookies {
        forward = "none"
      }
    }

    min_ttl     = 0
    default_ttl = 300
    max_ttl     = 3600
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn            = local.custom_domain_enabled ? aws_acm_certificate_validation.live[0].certificate_arn : null
    ssl_support_method             = local.custom_domain_enabled ? "sni-only" : null
    minimum_protocol_version       = local.custom_domain_enabled ? "TLSv1.2_2021" : null
    cloudfront_default_certificate = !local.custom_domain_enabled
  }
}

data "aws_iam_policy_document" "live_bucket_policy" {
  statement {
    actions = ["s3:GetObject"]
    resources = [
      "${aws_s3_bucket.live.arn}/*",
    ]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.live.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "live" {
  bucket = aws_s3_bucket.live.id
  policy = data.aws_iam_policy_document.live_bucket_policy.json
}
