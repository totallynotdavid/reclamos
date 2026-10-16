terraform {
  required_version = ">= 1.14.0"

  backend "s3" {
    bucket       = "reclamos-terraform-state"
    key          = "reclamos/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}
