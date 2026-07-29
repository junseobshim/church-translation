# Setup

Step-by-step setup instructions; for more details and troubleshooting information, see the [README](README.md).

## Prerequisites

- macOS with [Homebrew](https://brew.sh/)

## Install & configure

```bash
# Install dependencies (skip any you already have)
brew install python git portaudio cloudflared

# Clone the repo into your Documents folder.
# launcher.sh expects the repo at $HOME/Documents/church-translation, so clone from inside ~/Documents.
cd ~/Documents
git clone https://github.com/junseobshim/church-translation.git
cd church-translation

# Create a virtual environment and install Python packages
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Download correct .env file (live or testing) to repo root

# Log in to Cloudflare (opens a browser; creates ~/.cloudflared/ and cert.pem)
cloudflared tunnel login

# Download correct .json file (live or testing) into ~/.cloudflared/.
```



## Create the launcher app

1. Open **Automator** → **New** → **Application**
2. Add a **Run Shell Script** action and empty any contents
3. Paste the contents of `[launcher.sh](launcher.sh)` into **Run Shell Script**
4. Save application



## Done

Open app to launch control panel.