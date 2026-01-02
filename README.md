# Costco UK Stock Tracker

A self-hosted web application for tracking Costco UK product availability and prices with automated alerts and assisted checkout capabilities.

## Overview

This application monitors Costco UK products for stock changes and price drops, sending notifications through multiple channels when configured conditions are met. It provides a web interface for managing tracked products, viewing price history, and configuring alerts.

## Features

- **Product Monitoring**: Track stock status and prices for Costco UK products
- **Price History**: Historical price tracking with visual charts
- **Smart Alerts**: Configurable notifications for stock changes, price drops, and target prices
- **Multi-Channel Notifications**: Email (SMTP), Telegram, Discord, and Pushover support
- **Assisted Checkout**: Optional automatic basket addition when conditions are met
- **REST API**: Full API access for automation and integration
- **Data Export**: CSV and JSON export functionality
- **Responsive Interface**: Mobile-friendly web dashboard

## Requirements

- Python 3.8 or higher
- SQLite (included) or PostgreSQL (optional)
- Internet connection for accessing Costco UK website

## Quick Start

### Docker Installation (Recommended)

```bash
git clone https://github.com/yourusername/costco-tracker.git
cd costco-tracker

cp .env.example .env
# Edit .env with your preferred text editor

docker-compose up -d
```

Access the application at `http://localhost:8000`

### Manual Installation

```bash
git clone https://github.com/yourusername/costco-tracker.git
cd costco-tracker

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your settings

# Run setup wizard
python main.py --setup

# Start the application
python main.py
```

Access the application at `http://localhost:8000`

## Configuration

### Initial Setup

1. Navigate to `http://localhost:8000`
2. Set a site password when prompted
3. Log in with your password
4. Begin adding products to track

### Environment Variables

Configuration is managed through the `.env` file. Copy `.env.example` to `.env` and modify as needed.

#### Core Settings

```env
DEBUG=false
SECRET_KEY=generate-a-secure-random-string-here
SESSION_TIMEOUT_MINUTES=1440
DATABASE_URL=sqlite:///./data/costco_tracker.db
```

#### Scraper Settings

```env
DEFAULT_POLL_INTERVAL_MINUTES=45
MIN_POLL_INTERVAL_MINUTES=15
MAX_POLL_INTERVAL_MINUTES=180
REQUEST_TIMEOUT_SECONDS=30
SAFE_MODE=true
KILL_SWITCH=false
```

#### Email Notifications (SMTP)

```env
SMTP_ENABLED=true
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-email@gmail.com
SMTP_PASSWORD=your-app-password
SMTP_FROM_EMAIL=your-email@gmail.com
SMTP_USE_TLS=true
```

For Gmail, generate an App Password at https://support.google.com/accounts/answer/185833

#### Telegram Notifications

```env
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_CHAT_ID=your-chat-id
```

To get your chat ID, message @userinfobot on Telegram.

#### Discord Notifications

```env
DISCORD_ENABLED=true
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/your-webhook-url
```

Create webhooks via Server Settings > Integrations > Webhooks.

#### Pushover Notifications

```env
PUSHOVER_ENABLED=true
PUSHOVER_APP_TOKEN=your-app-token
PUSHOVER_USER_KEY=your-user-key
```

Register at https://pushover.net/ to obtain credentials.

### Assisted Checkout Configuration

**WARNING**: Automated login to Costco may violate their Terms of Service. This feature does NOT complete purchases automatically, only adds items to your basket. Use at your own risk.

To enable:

1. Encrypt your Costco password:
   ```bash
   python main.py --encrypt
   ```

2. Add to `.env`:
   ```env
   AUTO_ADD_TO_BASKET_ENABLED=true
   COSTCO_EMAIL=your-costco-email@example.com
   COSTCO_PASSWORD_ENCRYPTED=paste-encrypted-password-here
   ```

3. Enable auto-add on specific products through the web interface

## Usage

### Adding Products

Products can be added using either:

- **Full URL**: `https://www.costco.co.uk/Electronics/Televisions/p/12345`
- **Item Number**: `12345`

### Managing Tracked Products

From the product detail page, configure:

- Target price threshold
- Polling interval (15-180 minutes)
- Alert preferences (stock changes, price drops, target price)
- Assisted checkout settings

### API Access

The application exposes a REST API for automation:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/products` | GET | List all tracked products |
| `/api/products/{id}` | GET | Get product details |
| `/api/products/{id}/history` | GET | Get price history |
| `/api/export?format=csv` | GET | Export all data |
| `/api/scheduler/run` | POST | Trigger manual check |
| `/api/kill-switch/on` | POST | Stop all automation |

API documentation is available at `http://localhost:8000/api/docs`

## Running as a System Service

### Linux (systemd)

1. Edit the service file with your installation path:
   ```bash
   nano costco-tracker.service
   ```

2. Install the service:
   ```bash
   sudo cp costco-tracker.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable costco-tracker
   sudo systemctl start costco-tracker
   ```

3. Check status:
   ```bash
   sudo systemctl status costco-tracker
   sudo journalctl -u costco-tracker -f
   ```

## Project Structure

```
costco-tracker/
├── app/
│   ├── __init__.py
│   ├── config.py           # Configuration management
│   ├── models.py           # Database models
│   ├── database.py         # Database operations
│   ├── security.py         # Authentication and encryption
│   ├── scraper.py          # Costco website scraper
│   ├── notifications.py    # Alert system
│   ├── scheduler.py        # Background polling
│   ├── basket.py           # Assisted checkout
│   ├── routes.py           # API endpoints
│   ├── templates/          # HTML templates
│   └── static/             # CSS, JavaScript
├── data/                   # Database and logs (auto-created)
├── main.py                 # Application entry point
├── requirements.txt        # Python dependencies
├── .env.example            # Configuration template
├── Dockerfile
├── docker-compose.yml
├── install.sh              # Installation script
└── costco-tracker.service  # systemd service file
```

## Troubleshooting

### Access Forbidden Errors

The scraper may be blocked by Costco's anti-bot measures. Try:

- Enabling `SAFE_MODE=true` in `.env`
- Increasing `DEFAULT_POLL_INTERVAL_MINUTES`
- Verifying Costco UK is accessible from your server

### Products Not Updating

- Check the Status page for detailed error messages
- Manually refresh a product to test connectivity
- Review logs in `data/costco_tracker.log`

### Notification Issues

- Verify credentials in the Settings page
- Test your SMTP/Telegram/Discord configuration independently
- Check firewall rules for outbound connections

### Database Issues

For SQLite (default), ensure the `data/` directory has write permissions.

For PostgreSQL, verify the connection string in `DATABASE_URL`.

## Security Considerations

- Site access is password-protected by default
- Passwords are hashed using bcrypt
- Costco credentials (if used) are encrypted at rest with Fernet
- No payment information is stored
- Sessions expire after configured timeout (default: 24 hours)
- IP allowlist support available via `ALLOWED_IPS` environment variable

## Legal and Ethical Considerations

**IMPORTANT**:

- This tool is intended for personal use only
- Automated access to Costco's website may violate their Terms of Service
- The assisted checkout feature automates interaction with Costco's systems
- Users are responsible for compliance with applicable laws and terms of service
- Use responsibly and at your own risk

## License

MIT License - See LICENSE file for details.

## Contributing

Contributions are welcome. Please:

1. Fork the repository
2. Create a feature branch
3. Make your changes with clear commit messages
4. Test thoroughly
5. Submit a pull request

## Support

For issues and feature requests, please use the GitHub issue tracker.
