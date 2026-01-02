#!/bin/bash
# Costco UK Stock Tracker - Installation Script
# For Debian/Ubuntu Linux

set -e

echo "=================================================="
echo "Costco UK Stock Tracker - Installation"
echo "=================================================="
echo ""

# Check if running as root
if [ "$EUID" -eq 0 ]; then
    echo "Warning: Running as root. Consider using a regular user."
fi

# Check Python version
PYTHON_CMD=""
if command -v python3 &> /dev/null; then
    PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    PYTHON_CMD="python3"
elif command -v python &> /dev/null; then
    PYTHON_VERSION=$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    PYTHON_CMD="python"
else
    echo "Error: Python 3 is not installed."
    echo "Please install Python 3.10 or higher:"
    echo "  sudo apt update && sudo apt install python3 python3-pip python3-venv"
    exit 1
fi

echo "Found Python $PYTHON_VERSION"

# Check Python version is 3.10+
MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)

if [ "$MAJOR" -lt 3 ] || ([ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 10 ]); then
    echo "Error: Python 3.10 or higher is required."
    echo "Current version: $PYTHON_VERSION"
    exit 1
fi

# Get installation directory
INSTALL_DIR="${1:-$(pwd)}"
echo "Installation directory: $INSTALL_DIR"

# Create virtual environment
echo ""
echo "Creating virtual environment..."
$PYTHON_CMD -m venv "$INSTALL_DIR/venv"

# Activate virtual environment
source "$INSTALL_DIR/venv/bin/activate"

# Upgrade pip
echo "Upgrading pip..."
pip install --upgrade pip

# Install dependencies
echo "Installing dependencies..."
pip install -r "$INSTALL_DIR/requirements.txt"

# Create data directory
mkdir -p "$INSTALL_DIR/data"

# Create .env file if it doesn't exist
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo "Creating .env file from template..."
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"

    # Generate a random secret key
    SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    sed -i "s/change-this-to-a-random-string-at-least-32-chars/$SECRET_KEY/" "$INSTALL_DIR/.env"

    echo "Generated random SECRET_KEY"
fi

# Initialize database
echo ""
echo "Initializing database..."
$PYTHON_CMD -c "from app.database import init_db; init_db()"

echo ""
echo "=================================================="
echo "Installation complete!"
echo "=================================================="
echo ""
echo "Next steps:"
echo ""
echo "1. Edit the configuration file:"
echo "   nano $INSTALL_DIR/.env"
echo ""
echo "2. Run the setup wizard:"
echo "   source $INSTALL_DIR/venv/bin/activate"
echo "   python main.py --setup"
echo ""
echo "3. Start the server:"
echo "   python main.py"
echo ""
echo "4. (Optional) Install as a systemd service:"
echo "   sudo cp costco-tracker.service /etc/systemd/system/"
echo "   sudo systemctl enable costco-tracker"
echo "   sudo systemctl start costco-tracker"
echo ""
echo "Access the web UI at: http://localhost:8000"
echo ""
