#!/usr/bin/env bash
# 重装会议记录 skill 的运行环境和模型(约 2GB 下载)。已存在的部分会跳过。
set -euo pipefail
H="${MEETING_MINUTES_HOME:-$HOME/.local/share/meeting-minutes}"
M="$H/models"
mkdir -p "$M/whisper-large-v3-turbo" "$M/diarization"

if [ ! -x "$H/venv/bin/python" ]; then
  command -v uv >/dev/null || { echo "需要 uv:curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
  uv venv "$H/venv" --python 3.11
fi
uv pip install --python "$H/venv/bin/python" mlx-whisper sherpa-onnx av python-docx opencc-python-reimplemented pymupdf

cd "$M/whisper-large-v3-turbo"
for f in config.json weights.safetensors; do
  [ -s "$f" ] || curl -L --retry 3 -C - -o "$f" "https://huggingface.co/mlx-community/whisper-large-v3-turbo/resolve/main/$f"
done

cd "$M/diarization"
R=https://github.com/k2-fsa/sherpa-onnx/releases/download
[ -d sherpa-onnx-pyannote-segmentation-3-0 ] || { curl -L --retry 3 -o seg.tar.bz2 "$R/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2" && tar xjf seg.tar.bz2 && rm seg.tar.bz2; }
E=3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx
[ -s "$E" ] || curl -L --retry 3 -o "$E" "$R/speaker-recongition-models/$E"

cd "$M"
SV=sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09
[ -d sensevoice ] || { curl -L --retry 3 -o sv.tar.bz2 "$R/asr-models/$SV.tar.bz2" && tar xjf sv.tar.bz2 && mv "$SV" sensevoice && rm sv.tar.bz2; }

echo "完成。模型目录:"; du -sh "$M"/*
