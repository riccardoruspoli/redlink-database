variable "aws_region" {
  type        = string
  description = "AWS region for the runtime infrastructure."
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

variable "resource_prefix" {
  type        = string
  description = "Prefix used for naming AWS resources."
  default     = "redlink-prod"
}

variable "notification_email" {
  type        = string
  description = "Email address used for SNS notifications."
}

variable "public_hostname" {
  type        = string
  description = "Public hostname served by CloudFront."
}

variable "enable_custom_domain" {
  type        = bool
  description = "Whether to attach the ACM-backed custom hostname to CloudFront."
  default     = false
}

variable "language" {
  type        = string
  description = "Wiki language code used by the first cloud pipeline."
  default     = "en"
}

variable "runner_ami_id" {
  type        = string
  description = "AMI ID for the pre-baked runner."
}

variable "runner_instance_type" {
  type        = string
  description = "EC2 instance type for the pipeline runner."
  default     = "t4g.2xlarge"
}

variable "root_volume_size_gb" {
  type        = number
  description = "Size of the root gp3 volume for the runner."
  default     = 200
}

variable "duckdb_memory_limit" {
  type        = string
  description = "DuckDB memory limit passed to the application."
  default     = "24GB"
}

variable "duckdb_threads" {
  type        = number
  description = "DuckDB thread count passed to the application."
  default     = 4
}

variable "decompress_workers" {
  type        = number
  description = "Decompression worker count passed to the application."
  default     = 1
}

variable "parallel_workers" {
  type        = number
  description = "SQL to Parquet parallel worker count passed to the application."
  default     = 4
}

variable "parallel_chunk_insert_lines" {
  type        = number
  description = "Chunk insert line count passed to the application."
  default     = 100
}

variable "parallel_target_chunk_multiplier" {
  type        = number
  description = "Target chunk multiplier passed to the application."
  default     = 3
}

variable "web_batch_size" {
  type        = number
  description = "Web export batch size passed to the application."
  default     = 100000
}

variable "schedule_expression" {
  type        = string
  description = "EventBridge Scheduler cron expression."
  default     = "cron(0 7 * * ? *)"
}

variable "schedule_timezone" {
  type        = string
  description = "Scheduler timezone."
  default     = "Europe/Rome"
}

variable "state_machine_timeout_seconds" {
  type        = number
  description = "Timeout for the pipeline state machine."
  default     = 43200
}

variable "runner_janitor_grace_seconds" {
  type        = number
  description = "Additional grace period before janitor terminates orphaned runner instances."
  default     = 1800
}

variable "log_retention_days" {
  type        = number
  description = "CloudWatch log retention in days."
  default     = 30
}

variable "monthly_budget_limit_usd" {
  type        = string
  description = "Monthly account-level AWS budget in USD."
  default     = "30"
}

variable "artifacts_bucket_name" {
  type        = string
  description = "Bucket for versioned published artifacts."
}

variable "live_bucket_name" {
  type        = string
  description = "Bucket for live static-site serving."
}
