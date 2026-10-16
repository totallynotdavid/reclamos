variable "aws_region" {
  type        = string
  description = "AWS region for the Reclamos stack."
  default     = "us-east-1"
}

variable "project_name" {
  type        = string
  description = "Prefix used for AWS resource names."
  default     = "reclamos"
}

variable "db_username" {
  type        = string
  description = "RDS master username."
  default     = "reclamos"
}

variable "db_name" {
  type        = string
  description = "RDS database name."
  default     = "reclamos"
}

variable "api_domain_name" {
  type        = string
  description = "Public DNS name for the Reclamos API, covered by its ACM certificate."
}

variable "route53_zone_name" {
  type        = string
  description = "Public Route53 hosted zone containing api_domain_name."
}

variable "api_image_tag" {
  type        = string
  description = "Immutable ECR tag deployed to the API and worker tasks."
  default     = "bootstrap"

  validation {
    condition     = var.api_image_tag != "latest"
    error_message = "api_image_tag must be an immutable tag other than latest."
  }
}
