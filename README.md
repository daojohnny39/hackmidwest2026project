# hackmidwest2026project

## Front-facing automatic capture

The active first-round scope is **one automatic front-facing snapshot**, with live
guidance for face position and head angle. See the
[front capture specification](docs/front-capture-spec.md) for the rules, guided UI,
capture output, verification plan, and future **AWS Strands Stage 2 handoff**.

Yaw, pitch, and roll must each be within ±5° of front-facing neutral, and angle and
position checks must pass for 400 milliseconds before capture. Capture stops after
one successful photo; retake is explicit. Framing thresholds are initial settings
under test. The angle tolerance does not guarantee physical measurement accuracy.

The [earlier multi-perspective design](docs/automatic-capture-spec.md) is retained
as future reference; left/right capture is deferred. The automatic capture engine,
guidance, reset logging, and `useAutoCapture` integration hook are implemented.
The camera page uses automatic capture by default. Choose **Start camera**, follow
the positioning guidance, and hold steady; the app takes the photo once all checks
pass. The manual checkpoint remains available at `?mode=manual`. A future UI can
reuse the same capture state and actions without changing the agreed defaults.
Stage 1 produces a JPEG and metadata; a later backend adapter can hand that result
to the AWS Strands workflow. No upload or AWS execution is implemented.

## OpenCV and MediaPipe setup

Run from the project root using Python 3.13:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`.

MediaPipe requires `opencv-contrib-python`, which includes OpenCV's main and
extra modules. It still imports as `cv2`. Use only this OpenCV package in the
environment because the OpenCV variants share the same Python module.

If you already installed the previous `opencv-python` requirements, activate
that environment and migrate it with:

```bash
python -m pip uninstall -y opencv-python opencv-contrib-python
python -m pip install -r requirements.txt
```

Removing both variants first also repairs environments where both were installed.

Verify the installed packages:

```bash
python -c "import cv2, mediapipe as mp; print('OpenCV:', cv2.__version__); print('MediaPipe:', mp.__version__)"
python -m pip check
```

These setup commands install the libraries only. Download the separate Face
Landmarker model bundle below before initializing the detector.

## Step 3: Download the Face Landmarker model

Use Google's official float16 FaceLandmarker bundle from the
[Face Landmarker model page](https://developers.google.com/edge/mediapipe/solutions/vision/face_landmarker#models).
The Python package provides the API; the `.task` file contains the trained models
that run locally.

Run from the project root (macOS/Linux):

```bash
mkdir -p models
curl --fail --location --retry 3 \
  --output models/face_landmarker.task.part \
  'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task' \
  && mv models/face_landmarker.task.part models/face_landmarker.task
```

Alternatively, download the **FaceLandmarker** bundle from the model page and
save it as `models/face_landmarker.task`.

Downloaded and verified on October 10, 2026:

- Local path: `models/face_landmarker.task`
- Size: 3,758,596 bytes (about 3.76 MB)
- SHA-256: `64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff`
- Verification: checksum matched Google Storage's MD5 metadata; archive integrity
  and the three included TFLite model headers passed checks.

To check your download against this recorded SHA-256:

```bash
python -c "from pathlib import Path; import hashlib; print(hashlib.sha256(Path('models/face_landmarker.task').read_bytes()).hexdigest())"
```

The source URL uses `latest`, so a future Google update may change the size and
checksum. Downloaded `.task` files and incomplete `.task.part` downloads are
excluded from Git; each developer needs to download the model after cloning.

## Step 4: Initialize the Face Landmarker

With the virtual environment activated, run from the project root:

```bash
python -m face_landmarker
```

This loads the downloaded model, reports successful initialization, and closes
the detector. It does not open the camera or analyze a photo.

[`face_landmarker.py`](face_landmarker.py) exposes `create_face_landmarker()` with
these settings:

| Setting | Value |
| --- | --- |
| Model | `models/face_landmarker.task`, resolved relative to the Python file |
| Delegate | CPU (local processing) |
| Running mode | `IMAGE`, for independent photos |
| Maximum faces | `2`, so later processing can reject multiple detected faces |
| Facial transformation matrices | Enabled for later head-angle estimation |
| Blendshapes | Disabled |

The settings and context-manager lifecycle follow the
[MediaPipe Python guide](https://developers.google.com/edge/mediapipe/solutions/vision/face_landmarker/python).
To use the detector in subsequent processing:

```python
from face_landmarker import create_face_landmarker

with create_face_landmarker() as landmarker:
    # Reuse this instance for sequential photo requests (Step 5).
    pass
```

Create it once at application startup, reuse it for requests one at a time, and
close it during shutdown. Exiting the `with` block closes it even if processing
raises an exception. If managing its lifetime directly, call `landmarker.close()`
in a `finally` block. Importing the module does not initialize a detector.

For a different local model location, pass a path to `create_face_landmarker(path)`
or run `python -m face_landmarker --model /path/to/face_landmarker.task`.
A missing model reports its resolved path and directs you to Step 3.

## Step 5 checkpoint: Analyze your photo

Use a JPEG or PNG of yourself or another consenting participant. With the
virtual environment activated, run from the project root:

```bash
python -m photo_checkpoint "/absolute/path/to/your/photo.jpg"
```

The command reads the photo with OpenCV, converts BGR pixels to RGB, and runs the
local Face Landmarker. It prints the detected face count, landmark counts, and
number of transformation matrices. Your source photo stays unchanged.

Each run creates a separate `checkpoints/photo-.../` directory containing:

- `landmarks.png`: a copy of the photo with landmark points drawn on each face.
- `results.json`: image dimensions, face count, normalized landmark coordinates,
  and facial transformation matrices.

Open the overlay to check that the points line up with the eyes, nose, mouth,
and face outline. For a single-person photo, the checkpoint passes when one face
is detected and its landmarks align visually. Zero faces means detection did
not succeed for that photo; try clearer lighting or a larger face. The detector
returns at most two faces, so its count is capped at two.

Processing runs locally. The default `checkpoints/` output directory and the
optional `photos/` input directory are ignored by Git. You can also keep your
photo outside the repository. To choose another output parent directory, add
`--output-dir /path/to/output`; results there are not covered by these ignore rules.

`photo_checkpoint.analyze_image(image_bytes, landmarker)` can reuse the detector
from Step 4 for subsequent images. This checkpoint produces geometry only;
head-angle calculations are a later step.

## Connect the browser camera

The teammate's camera UI now analyzes each captured photo directly in the browser
using MediaPipe's JavaScript/WebAssembly runtime and the downloaded model. The
Python checkpoint above remains a separate local verification tool.

```bash
cd frontend
npm install
npm run dev
```

Open the local Vite URL and choose **Start camera**. Follow the guidance; the
front photo is captured automatically once angle and framing checks pass for
400 ms. Download the photo and metadata, or choose **Retake front photo**.
Use `?mode=manual` for the separate checkpoint with landmark overlays and analysis JSON.
Model assets are copied automatically before development and
production builds. See the [frontend README](frontend/README.md) for details and
browser tests. This connection requires no Python endpoint or Vite API proxy.

## Run the tests

With the virtual environment activated:

```bash
python -m unittest discover -s tests -v
```

The OpenCV tests check BGR-to-RGB color conversion and JPEG saving and loading
using synthetic images. Detector and checkpoint tests cover missing models,
invalid images, RGB input, landmark overlays, detector reuse, and saved results.
Tests that run the real detector require the downloaded model and are skipped
when it is absent. All tests use Python's built-in `unittest` module and require
no camera, personal photos, or additional testing package. Temporary images are
automatically deleted after the test.
