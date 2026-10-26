# Third-party software and models

Nothing here is bundled except the small BandIt model code; the rest is downloaded on first run from its
original source. Licences are the ones published by each project, to the best of my knowledge. Check them if you
plan to use the output commercially.

| Component | Used for | Source | Licence |
|---|---|---|---|
| yt-dlp | downloading | https://github.com/yt-dlp/yt-dlp | Unlicense |
| Flask | the local web server | https://flask.palletsprojects.com | BSD-3-Clause |
| FFmpeg | video and audio processing | https://ffmpeg.org | LGPL/GPL (depends on the build installed) |
| PyTorch, torchaudio | running the AI models | https://pytorch.org | BSD-style |
| Music-Source-Separation-Training (`tools/sep`, BandIt code only) | background-music removal code | https://github.com/ZFTurbo/Music-Source-Separation-Training | MIT (see `tools/sep/LICENSE`) |
| BandIt Plus model weights (speech / music / effects) | background-music removal | release v.1.0.3 of the repository above | **not confirmed**: the project does not state one |
| RapidOCR | reading Chinese captions | https://github.com/RapidAI/RapidOCR | Apache-2.0 |
| Helsinki-NLP/opus-mt-zh-en | Chinese to English translation | https://huggingface.co/Helsinki-NLP/opus-mt-zh-en | CC-BY-4.0 |
| Kokoro-82M (via kokoro-onnx) | English voices | https://huggingface.co/hexgrad/Kokoro-82M, https://github.com/thewh1teagle/kokoro-onnx | Apache-2.0 |
| transformers, onnxruntime, OpenCV, NumPy | supporting libraries | their project pages | Apache-2.0 / MIT / BSD |
