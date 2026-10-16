output "api_repository" {
  value = aws_ecr_repository.api.repository_url
}

output "pdf_bucket" {
  value = aws_s3_bucket.pdfs.bucket
}

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "api_load_balancer_hostname" {
  value = aws_lb.api.dns_name
}

output "api_url" {
  value = "https://${var.api_domain_name}"
}
