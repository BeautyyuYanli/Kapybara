---
name: read-video
description: Extracts video frames at 1 FPS, overlays timestamps, assembles them into grid images, and prints the absolute output image paths. Complement to the read_media tool.
---

# read-video

## Usage

Extracts frames from a video at a rate of 1 frame per second, overlays timestamps, then outputs images in chunks of up to 9 frames:

- **Chunk size = 1**: outputs a single timestamped frame path (no grid).
- **Chunk size = 2–4**: outputs a **2x2** grid image path (canvas size `w*2` by `h*2`).
- **Chunk size = 5–9**: outputs a **3x3** grid image path (canvas size `w*3` by `h*3`).

**Important Rule**: You **must** call the `read_media` tool on all output image paths immediately after the script finishes.

### Interface

```bash
~/.kapybara/skills/core/read-video/read <video_path>
```

### Behavior

- Processes videos up to **45 minutes** long.
- Outputs **absolute output image paths** to stdout, one per line (grid images or a timestamped single frame).
- Fails with a non-zero exit code (e.g., code 3 for duration limit) if the video is too long or extraction fails.
