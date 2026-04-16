resource "aws_launch_template" "runner" {
  name_prefix   = "${var.resource_prefix}-runner-"
  image_id      = var.runner_ami_id
  instance_type = var.runner_instance_type

  update_default_version = true

  iam_instance_profile {
    arn = aws_iam_instance_profile.runner.arn
  }

  instance_initiated_shutdown_behavior = "terminate"

  monitoring {
    enabled = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 2
    http_tokens                 = "required"
  }

  network_interfaces {
    associate_public_ip_address = true
    security_groups             = [aws_security_group.runner.id]
    subnet_id                   = aws_subnet.public_a.id
  }

  block_device_mappings {
    device_name = "/dev/sda1"

    ebs {
      delete_on_termination = true
      encrypted             = true
      volume_size           = var.root_volume_size_gb
      volume_type           = "gp3"
    }
  }

  tag_specifications {
    resource_type = "instance"

    tags = merge(local.common_tags, {
      Name = "${var.resource_prefix}-runner"
      Role = "runner"
    })
  }

  tag_specifications {
    resource_type = "volume"

    tags = merge(local.common_tags, {
      Name = "${var.resource_prefix}-runner-root"
      Role = "runner"
    })
  }
}
