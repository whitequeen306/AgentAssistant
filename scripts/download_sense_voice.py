"""Download the SenseVoice-Small ONNX model for sherpa-onnx ASR.

Uses the k2-fsa GitHub release bundle with a ghfast.top mirror for China —
NOT HuggingFace (often blocked / 401'd by corporate proxies). The bundle is a
tar.bz2 that extracts to a dir with model.onnx + tokens.txt (+ am.mvn + samples).
STTEngine finds the model regardless of exact filename (int8 → full → rglob).

If `truststore` is installed, this script uses the Windows certificate store
(may bypass a corporate TLS-intercepting proxy that breaks plain certifi SSL).

Usage:
    python scripts/download_sense_voice.py

If Python still can't reach the network (proxy 401 / SSL), download via BROWSER:
    https://ghfast.top/https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2
then extract the tar.bz2 into  <data_dir>/models/sense-voice/  (Windows 10+:
`tar -xjf sense-voice.tar.bz2` works natively).

Verify after download:
    python -c "from agent_assistant.voice.stt import STTEngine as E; print(E().transcribe_bytes(b'\\x00\\x00'*16000))"
"""
import sys
import tarfile
import urllib.request
from pathlib import Path

# LianYu-PC's proven URLs: ghfast.top China mirror first, github.com fallback.
URLS = [
    "https://ghfast.top/https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2",
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2",
]


def _download(dest: Path) -> bool:
    for url in URLS:
        print(f"trying {url}")
        try:
            urllib.request.urlretrieve(url, dest)
            return True
        except Exception as e:
            print(f"  failed: {e}")
    return False


def main() -> None:
    # Use the Windows cert store if available (works around corporate TLS interception)
    try:
        import truststore

        truststore.set_default_context()
        print("using OS native trust store (truststore)")
    except ImportError:
        print("truststore not installed (pip install truststore) — using certifi")

    from agent_assistant.config import settings

    target = settings.data_dir / "models" / "sense-voice"
    target.mkdir(parents=True, exist_ok=True)
    archive = target / "sense-voice.tar.bz2"
    print(f"target dir: {target}")

    if not _download(archive):
        print("\nAll Python download sources failed. Use the BROWSER instead:")
        for u in URLS:
            print(f"  {u}")
        print(f"Then extract into: {target}  (Windows 10+: tar -xjf <file>.tar.bz2)")
        sys.exit(1)

    print("extracting ...")
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(target)
    archive.unlink(missing_ok=True)

    print("done. Model files now present:")
    for p in sorted(target.rglob("*")):
        if p.is_file() and p.suffix in {".onnx", ".txt", ".mvn"}:
            print(f"  {p}")
    print("\nVerify with:")
    print('  python -c "from agent_assistant.voice.stt import STTEngine as E; '
          'print(E().transcribe_bytes(b\'\\x00\\x00\'*16000))"')


if __name__ == "__main__":
    main()
