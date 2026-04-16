variable "aws_region" {
  type        = string
  description = "AWS region for the Terraform backend bootstrap."
  default     = "us-east-1"
}

variable "project" {
  type        = string
  description = "Project name."
  default     = "redlink"
}

variable "environment" {
  type        = string
  description = "Deployment environment."
  default     = "prod"
}

variable "terraform_state_bucket_name" {
  type        = string
  description = "S3 bucket name for Terraform remote state."
}

variable "terraform_lock_table_name" {
  type        = string
  description = "DynamoDB table name for Terraform state locking."
}
