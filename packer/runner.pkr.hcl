packer {
  required_plugins {
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = ">= 1.3.0"
    }
  }
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "ami_name_prefix" {
  type    = string
  default = "redlink-prod-runner-arm64"
}

source "amazon-ebs" "runner" {
  ami_name                                  = "${var.ami_name_prefix}-{{timestamp}}"
  instance_type                             = "t4g.small"
  region                                    = var.aws_region
  ssh_username                              = "ubuntu"
  temporary_security_group_source_public_ip = true

  source_ami_filter {
    filters = {
      name                = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    most_recent = true
    owners      = ["099720109477"]
  }

  tags = {
    Project     = "redlink"
    Environment = "prod"
    ManagedBy   = "packer"
  }
}

build {
  name    = "redlink-runner"
  sources = ["source.amazon-ebs.runner"]

  provisioner "shell" {
    inline = [
      "mkdir -p /tmp/redlink-build",
    ]
  }

  provisioner "file" {
    source      = "pyproject.toml"
    destination = "/tmp/redlink-build/pyproject.toml"
  }

  provisioner "file" {
    source      = "uv.lock"
    destination = "/tmp/redlink-build/uv.lock"
  }

  provisioner "file" {
    source      = "README.md"
    destination = "/tmp/redlink-build/README.md"
  }

  provisioner "file" {
    source      = "LICENSE"
    destination = "/tmp/redlink-build/LICENSE"
  }

  provisioner "file" {
    source      = "src"
    destination = "/tmp/redlink-build/"
  }

  provisioner "shell" {
    script = "packer/scripts/install_runner.sh"
  }
}
