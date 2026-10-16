resource "aws_s3_bucket" "pdfs" {
  bucket = "${var.project_name}-resolution-pdfs"
}

resource "aws_s3_bucket_public_access_block" "pdfs" {
  bucket                  = aws_s3_bucket.pdfs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "pdfs" {
  bucket = aws_s3_bucket.pdfs.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_ecr_repository" "api" {
  name                 = "${var.project_name}/api"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}
