# CyberPI
*Your private investigator for personal information.*

## Member 3: candidate image processing

Stage A's backend module validates retrieved JPEG/PNG images, detects up to two
faces using the existing MediaPipe code, stores normalized crops and returns
quality/review flags with source provenance. [Stage B](docs/member-3-stage-b.md)
adds server-side Jev contextual relevance, source categories, duplicate-record
reuse and stable ordering while preserving uncertain results for review.
[Stage C](docs/member-3-stage-c.md) connects both stages through one callable,
an authenticated local FastAPI adapter and a Strands tool, with a combined batch
deadline and exact-image analysis reuse. Its evaluation runner includes 40
synthetic cases, provisional labels and an explicitly mocked offline report.
Live Jev acceptance needs a server-side `TYPESAFE_API_KEY`; human label review
and live accuracy evaluation remain outstanding.
Quality thresholds are provisional; authorized real-photo acceptance is pending.

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
pass. Uncheck **Automatic capture** beside **Mirror preview** for a single-photo manual reference, or use `?capture=manual`.
The original manual checkpoint remains available at `?mode=manual`.
Stage 1 produces a JPEG and metadata; a later backend adapter can hand that result
to the AWS Strands workflow. No upload or AWS execution is implemented.

## OpenCV and MediaPipe setup

CyberPI helps users investigate where their likeness appears online and what
that exposure could mean for their personal information.

The project's goal is to let a user take a picture with the camera built into
the web application, use facial recognition to find public images of that user,
label the certainty of each match, and assess the **Threat Level** of where each
image was found. Potential concerns include sketchy websites, impersonation,
and possible catfishing.

## Intended workflow

1. **Capture a photo:** take a picture of yourself using the in-app camera.
2. **Find public images:** search for publicly accessible images that may show you
   using facial recognition.
3. **Review match certainty:** label how confidently each result matches your face.
4. **Assess the Threat Level:** evaluate the source and context of each result for
   signs of suspicious use, including possible catfishing or impersonation.

Match certainty and Threat Level answer different questions: how likely the
image is to show you, and how concerning its appearance on that site may be.

## Current implementation

The repository currently implements the camera and local face-analysis foundation:

- Browser camera access, camera switching, and photo capture.
- MediaPipe Face Landmarker analysis of each captured photo, including face
  counts, facial landmarks, and transformation matrices.
- Automatic capture integrated with optional subject details and a preview of fictional findings; manual capture requires one reference photo.
- Automatic photo/metadata downloads and a manual landmark overlay; the original manual checkpoint also exports the photo, overlay, and analysis JSON.
- Separate Python tools for initializing the detector and analyzing a local photo.

Public-image search, facial identity matching, match-certainty labels, and Threat
Level assessment are planned features. The existing landmark detector locates
facial geometry; it does not identify a person or search the web.

Current browser analysis runs on the user's device. Captured photos stay in
browser memory until deleted or the page is closed, and analysis does not upload
them. The web app currently runs without a Python backend or API credentials.

## Technologies used

| Technology | Role |
| --- | --- |
| React 19 + TypeScript | Web application and camera interface |
| Vite 8 | Frontend development server and production builds |
| Browser MediaDevices API + Canvas | Camera streams, photo capture, and overlays |
| MediaPipe Tasks Vision + WebAssembly | Local facial-landmark analysis in the browser |
| Python + MediaPipe | Standalone detector and photo-analysis tools |
| OpenCV (`opencv-contrib-python`) + NumPy | Image decoding, color conversion, and landmark overlays in Python |
| Playwright | Browser integration tests with simulated camera inputs |
| Oxlint + Python `unittest` | Frontend linting and Python tests |

## Run a development environment

Run the setup commands from the repository root unless indicated otherwise.
The commands below use a macOS/Linux shell.

### Step 1: Prerequisites

- Node.js **20.19+ in the 20.x line, or 22.12+**, with npm, as required by the
  installed Vite version.
- A browser and a camera for the interactive capture flow.
- `curl` for the model download, or download the file manually from the URL below.
- Python **3.12+ (tested on Python 3.12)** if you want to use the optional Python tools.

### Step 2: Install frontend dependencies

```bash
cd frontend
npm ci
cd ..
```

### Step 3: Download the Face Landmarker model

Both the web app and Python tools use `models/face_landmarker.task`. The model is
excluded from Git and must be downloaded after cloning:

```bash
mkdir -p models
curl --fail --location --retry 3 \
  --output models/face_landmarker.task.part \
  'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task' \
  && mv models/face_landmarker.task.part models/face_landmarker.task
```

Alternatively, download that URL in your browser and save the file as
`models/face_landmarker.task` in the repository root.

### Step 4: Start the web app

```bash
cd frontend
npm run dev
```

Open the URL printed by Vite (normally `http://localhost:5173`). Choose **Start
camera**, accept consent, and allow camera access. Follow the guidance and hold
steady to capture one photo automatically. Uncheck **Automatic capture** to take one photo
yourself and review its face count and landmark overlay. Subject details are
optional; a valid photo and authorization enable **Submit**.
The preview uses fictional findings and does not upload the photo.

The development and build scripts automatically copy the model and MediaPipe
WebAssembly files into `frontend/public/mediapipe/`. Production builds include
these assets, so analysis does not need a CDN at runtime.

Camera access requires a secure browser context. `localhost` works for local
development; testing from a phone or another device requires serving the app
over HTTPS. See the [frontend README](frontend/README.md) for camera details and
additional browser-test instructions.

## Optional Python development tools

From the repository root, confirm that `python3 --version` reports Python 3.12
or newer, then create a virtual environment and install the pinned dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows PowerShell, confirm the version with `py -3 --version`, create the
environment with `py -3 -m venv .venv`, and activate it with
`.venv\Scripts\Activate.ps1`.

Use only `opencv-contrib-python` in this environment; the other OpenCV package
variants share its `cv2` module. If migrating an environment with an older
`opencv-python` installation, uninstall both variants and reinstall the requirements:

```bash
python -m pip uninstall -y opencv-python opencv-contrib-python
python -m pip install -r requirements.txt
```

With the model downloaded in Step 3, verify detector initialization:

```bash
python -m face_landmarker
```

Analyze a local JPEG or PNG:

```bash
python -m photo_checkpoint "/absolute/path/to/your/photo.jpg"
```

Each run writes a landmark overlay (`landmarks.png`) and an analysis report
(`results.json`) into a new `checkpoints/photo-.../` directory. The report includes
image dimensions, face counts, landmarks, and transformation matrices. The
detector processes individual images on the CPU and returns at most two faces.
The `checkpoints/`, `photos/`, and downloaded model files are ignored by Git.

## Build and validation

From `frontend/`:

```bash
npm run lint
npm run build
npm run preview
```

The build runs TypeScript checks and writes the production app to `frontend/dist/`.
The preview command serves that build locally.

To run browser tests, install the Playwright engines and select a project:

```bash
npx playwright install firefox webkit
npm test -- --project=firefox
npm test -- --project=webkit
```

Open the local Vite URL and choose **Start camera**. Follow the guidance; the
front photo is captured automatically once angle and framing checks pass for
400 ms. Download the photo and metadata, or choose **Retake front photo**.
Use `?mode=manual` for the separate checkpoint with landmark overlays and analysis JSON.
Model assets are copied automatically before development and
production builds. See the [frontend README](frontend/README.md) for details and
browser tests. This connection requires no Python endpoint or Vite API proxy.

## Run the tests

From the repository root, with the Python environment activated:

```bash
python -m unittest discover -s tests -v
```

Tests that use the real Python detector are skipped if the model is absent.

## Agent pipeline

The search side is the Python package in `pipeline/`. One AWS Strands agent per
captured photo searches the web for the face, scrapes the pages it finds, and the
pipeline then groups the accounts into candidate profiles, scores each candidate, and
writes a final profile. It runs from the command line and isn't connected to the web
app yet. Setup, API keys, search backends, and the local mock internet it's tested
against are in [docs/agent-pipeline.md](docs/agent-pipeline.md). The build spec is
[docs/agent-pipeline-spec.md](docs/agent-pipeline-spec.md).

## Repository layout

| Path | Contents |
| --- | --- |
| `frontend/` | React app, camera hook, browser face analysis, and Playwright tests |
| `face_landmarker.py` | Reusable Python detector setup and initialization check |
| `photo_checkpoint.py` | Local photo analysis, landmark overlay, and JSON export |
| `requirements.txt` | Pinned Python dependencies |
| `pipeline/` | Agent pipeline: search, scraping, profiles, verification, and summary |
| `mock_internet/` | Local internet simulator for offline pipeline tests |
| `docs/` | Capture and agent pipeline specifications |
| `tests/` | Python image-processing, detector, mock internet, and pipeline tests |
| `models/` | Downloaded Face Landmarker model (ignored by Git) |
