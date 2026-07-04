#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "sudo bash install-ubuntu.sh 로 실행해 주세요."
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PLEX_PREFERENCES="/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Preferences.xml"
INSTALL_DIR="/opt/plex-ai-upscale"
DATA_DIR="/var/lib/plex-ai-upscale"
DOWNLOAD_URL="https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-ubuntu.zip"

apt-get update
apt-get install -y curl ffmpeg mesa-vulkan-drivers python3 unzip

install -d -m 0755 "${INSTALL_DIR}"
install -d -o plex -g plex -m 0750 "${DATA_DIR}" "${DATA_DIR}/work"
install -m 0755 "${SCRIPT_DIR}/server.py" "${INSTALL_DIR}/server.py"

TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TEMP_DIR}"' EXIT
curl -fL "${DOWNLOAD_URL}" -o "${TEMP_DIR}/realesrgan.zip"
unzip -q "${TEMP_DIR}/realesrgan.zip" -d "${TEMP_DIR}/realesrgan"
BINARY="$(find "${TEMP_DIR}/realesrgan" -type f -name realesrgan-ncnn-vulkan | head -n 1)"
if [[ -z "${BINARY}" ]]; then
  echo "Real-ESRGAN 실행 파일을 찾지 못했습니다."
  exit 1
fi
BINARY_DIR="$(dirname -- "${BINARY}")"
rm -rf "${INSTALL_DIR}/realesrgan"
cp -a "${BINARY_DIR}" "${INSTALL_DIR}/realesrgan"
chmod 0755 "${INSTALL_DIR}/realesrgan/realesrgan-ncnn-vulkan"

PLEX_TOKEN="${PLEX_TOKEN:-}"
if [[ -z "${PLEX_TOKEN}" && -r "${PLEX_PREFERENCES}" ]]; then
  PLEX_TOKEN="$(
    python3 - "${PLEX_PREFERENCES}" <<'PY'
import sys
import xml.etree.ElementTree as ET
print(ET.parse(sys.argv[1]).getroot().get("PlexOnlineToken", ""))
PY
  )"
fi
if [[ -z "${PLEX_TOKEN}" ]]; then
  read -r -p "Plex 토큰을 입력하세요: " PLEX_TOKEN
fi
if [[ -z "${PLEX_TOKEN}" ]]; then
  echo "Plex 토큰이 없어 설치를 중단합니다."
  exit 1
fi

cat > /etc/plex-ai-upscale.env <<EOF
PLEX_URL=http://127.0.0.1:32400
PLEX_TOKEN=${PLEX_TOKEN}
OUTPUT_DIR=${DATA_DIR}
WORK_DIR=${DATA_DIR}/work
REALESRGAN_BIN=${INSTALL_DIR}/realesrgan/realesrgan-ncnn-vulkan
REALESRGAN_MODEL_DIR=${INSTALL_DIR}/realesrgan/models
AI_MODEL=realesrgan-x4plus
AI_TILE_SIZE=128
MAX_CACHE_GB=100
EOF
chown root:plex /etc/plex-ai-upscale.env
chmod 0640 /etc/plex-ai-upscale.env

install -m 0644 \
  "${SCRIPT_DIR}/plex-ai-upscale.service" \
  /etc/systemd/system/plex-ai-upscale.service
systemctl daemon-reload
systemctl enable --now plex-ai-upscale.service

echo
echo "설치 완료. 상태 확인:"
echo "  systemctl status plex-ai-upscale --no-pager"
echo "  curl http://127.0.0.1:32600/health"
echo
echo "UFW 사용 중이면 Android 기기의 내부망 접속을 허용하세요:"
echo "  sudo ufw allow from 192.168.0.0/16 to any port 32600 proto tcp"
