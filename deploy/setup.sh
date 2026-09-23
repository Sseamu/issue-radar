#!/usr/bin/env bash
# Ubuntu 22.04/24.04 서버에서 한 번 실행: bash deploy/setup.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

echo "[1/5] 패키지 설치"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip sqlite3

echo "[2/5] 시간대 Asia/Seoul + 스왑 2GB (RAM 1GB 서버 보호)"
sudo timedatectl set-timezone Asia/Seoul
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "[3/5] 파이썬 가상환경"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements-server.txt

echo "[4/5] .env 확인"
if [ ! -f .env ]; then
  cp .env.server.example .env
  echo "  → .env 파일을 만들었습니다. 토큰/키를 입력한 뒤 이 스크립트를 다시 실행하세요:  nano .env"
  exit 0
fi

echo "[5/5] systemd 서비스 등록 (재부팅·오류 시 자동 재시작)"
sed -e "s#__USER__#$(whoami)#" -e "s#__DIR__#$DIR#g" deploy/radar-bot.service | sudo tee /etc/systemd/system/radar-bot.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now radar-bot
sleep 3
sudo systemctl --no-pager status radar-bot | head -n 8
echo "완료. 로그 보기: journalctl -u radar-bot -f"
