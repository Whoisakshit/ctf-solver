# CTF Solver

AI-powered multi-agent framework for solving Capture The Flag (CTF) challenges.

## ✨ Features

- 🤖 Multi-agent CTF solving
- 🧠 Claude, Codex & Gemini agent support
- 🔄 Central coordinator for agent orchestration
- 🔍 CTF utilities and tools
- 🐳 Sandboxed challenge execution
- ⚡ Iterative challenge-solving workflow
- 🖥️ Web GUI and CLI interface

## 🏗️ Architecture

```text
                         ┌──────────────┐
                         │ CTF Challenge│
                         └──────┬───────┘
                                │
                                ▼
                         ┌──────────────┐
                         │ Coordinator  │
                         └──────┬───────┘
                                │
              ┌─────────────────┼─────────────────┐
              ▼                 ▼                 ▼
        ┌──────────┐      ┌──────────┐      ┌──────────┐
        │  Claude  │      │  Codex   │      │  Gemini  │
        │  Solver  │      │  Solver  │      │  Solver  │
        └─────┬────┘      └─────┬────┘      └─────┬────┘
              │                 │                 │
              └─────────────────┼─────────────────┘
                                ▼
                         ┌──────────────┐
                         │ Tools/Sandbox│
                         │ Docker       │
                         └──────┬───────┘
                                │
                                ▼
                         ┌──────────────┐
                         │    Result    │
                         └──────────────┘
🛠️ Tech Stack
Python
UV
Claude
OpenAI / Codex
Google Gemini
Docker / Docker Desktop
Multi-Agent Architecture
📸 Screenshots
<!-- Add project screenshots here -->
🚀 Installation

Clone the repository:

git clone https://github.com/Whoisakshit/ctf-solver.git
cd ctf-solver

Install dependencies:

uv sync

Create your environment file:

cp .env.example .env

Add the required API keys and configuration to .env.

🐳 Docker

Docker Desktop is used for sandboxed challenge execution.

Make sure Docker Desktop is installed and running before starting the solver.

▶️ Running the Project
Web GUI

Start the web interface with:

uv run ctf-web

The application will start on localhost. Open the local URL shown in the terminal.

CLI
uv run python -m backend.cli
📁 Project Structure
ctf-solver/
├── backend/
│   ├── agents/
│   ├── tools/
│   └── ...
├── sandbox/
├── pull_challenges.py
├── pyproject.toml
├── uv.lock
├── .env.example
└── README.md

Challenge files are kept locally and are not included in this repository.

🔗 Links
GitHub Repository -https://github.com/Whoisakshit/route-optimizer

👨‍💻 Author
Akshit Khurana