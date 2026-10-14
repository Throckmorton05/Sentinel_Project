# Third-party notices

Sentinel's original work is licensed under GPL-3.0-only; see [LICENSE](LICENSE)
and [NOTICE.md](NOTICE.md). Third-party components below keep their own licenses.
This document records attribution and currently known terms; it is not a grant
of rights to components whose upstream terms remain unresolved.

## HaGeZi Social Networks DNS data

- **Purpose:** Social-media domain category and offline fallback
- **Upstream:** https://github.com/hagezi/dns-blocklists
- **Live source:** https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/social.txt
- **Snapshot bundled:** 2026-10-09
- **Author:** HaGeZi and upstream contributors
- **License:** GPL-3.0
- **License text:** `sentinel/dns/data/LICENSE-hagezi.txt`

Sentinel bundles a social-list snapshot, adds BeReal and Threads domains, and
excludes YouTube from this category. Source downloads continue automatically.

## OpenNSFW2

- **Purpose:** First-stage NSFW classifier
- **Upstream:** https://github.com/bhky/opennsfw2
- **Version:** 0.18.0
- **Author:** Bosco Yung
- **License:** MIT
- **License text:** `licenses/LICENSE-opennsfw2.txt`

**Sentinel usage**

Sentinel uses the official pretrained OpenNSFW2 model as the first stage of its image moderation pipeline. The official pretrained model has been converted to ONNX format for improved inference performance and is redistributed as part of Sentinel.

---

## NudeNet

- **Purpose:** Detection of exposed anatomical features in real images
- **Upstream:** https://github.com/notAI-tech/NudeNet
- **Version:** 3.4.2
- **Author:** Bedapudi Praneeth
- **License:** MIT
- **License text:** `licenses/LICENSE-nudenet.txt`

**Sentinel usage**

Sentinel uses the published NudeNet Python package (version 3.4.2) as the second-stage classifier for real photographic content.

---

## vit-base-patch16-224-hentai

- **Purpose:** Hentai image classification
- **Upstream:** https://huggingface.co/carbon225/vit-base-patch16-224-hentai
- **Version:** Model release bundled with Sentinel
- **Author:** carbon225
- **License:** CC0-1.0
- **License text:** `licenses/LICENSE-vit-base-patch16-224-hentai.txt`

**Sentinel usage**

Sentinel converts the published ViT weights to ONNX and runs the classifier locally through ONNX Runtime. The model retains its original `safe`, `suggestive`, and `explicit` output classes.


## anime_real_cls

- **Purpose:** Anime vs. real image classification
- **Upstream:** https://huggingface.co/deepghs/anime_real_cls
- **Version:** mobilenetv3_v1.4_dist
- **Author:** DeepGHS
- **License text:** Not provided by the upstream repository.
- **License:** OpenRAIL (as specified by the Hugging Face model card)

**Sentinel usage**

Sentinel uses the `mobilenetv3_v1.4_dist` model to distinguish anime-style imagery from real photographs as part of its image moderation pipeline.

**Note**

The upstream model card specifies the license as `OpenRAIL` but does not include the corresponding license text or identify the specific OpenRAIL variant.
