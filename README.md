# hackmidwest2026project

## OpenCV setup

Run from the project root using Python 3.13:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`.

## Run the OpenCV tests

With the virtual environment activated:

```bash
python -m unittest discover -s tests -v
```

The tests check BGR-to-RGB color conversion and JPEG saving and loading using
synthetic images. They use Python's built-in `unittest` module and require no
camera, MediaPipe model, or additional testing package. Temporary images are
automatically deleted after the test.
