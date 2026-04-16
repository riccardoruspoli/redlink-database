#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
export UV_INSTALL_DIR="${HOME}/.local/bin"

sudo apt-get update
sudo apt-get install -y \
  build-essential \
  ca-certificates \
  curl \
  git \
  snapd \
  unzip

curl -LsSf https://astral.sh/uv/install.sh | sh
sudo install -m 0755 "${UV_INSTALL_DIR}/uv" /usr/local/bin/uv

tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT

curl "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o "${tmp_dir}/awscliv2.zip"
unzip -q "${tmp_dir}/awscliv2.zip" -d "${tmp_dir}"
sudo "${tmp_dir}/aws/install" --bin-dir /usr/local/bin --install-dir /usr/local/aws-cli --update

sudo snap install amazon-ssm-agent --classic
sudo systemctl enable snap.amazon-ssm-agent.amazon-ssm-agent.service
sudo systemctl start snap.amazon-ssm-agent.amazon-ssm-agent.service

sudo mkdir -p /opt/redlink-database
sudo cp -R /tmp/redlink-build/src /opt/redlink-database/
sudo cp /tmp/redlink-build/pyproject.toml /opt/redlink-database/
sudo cp /tmp/redlink-build/uv.lock /opt/redlink-database/
sudo cp /tmp/redlink-build/README.md /opt/redlink-database/
sudo cp /tmp/redlink-build/LICENSE /opt/redlink-database/
sudo chown -R "${USER}:${USER}" /opt/redlink-database

cd /opt/redlink-database
uv python install 3.14
uv venv --python 3.14 /opt/redlink-database/.venv
uv pip install --python /opt/redlink-database/.venv/bin/python /opt/redlink-database

sudo ln -sf /opt/redlink-database/.venv/bin/redlink-database /usr/local/bin/redlink-database

sudo mkdir -p /data
sudo chown -R "${USER}:${USER}" /data /opt/redlink-database
sudo apt-get clean
