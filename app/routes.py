"""
FastAPI routes for the web interface and API.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path

from fastapi import (
    FastAPI, Request, Response, Depends, HTTPException,
    Form, Query, status
)
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.config import settings
from app.database import get_db, init_db
from app.security import PasswordManager, SessionManager, check_ip_allowed
from app.models import (
    Product, PriceHistory, StockHistory, Alert, SchedulerStatus,
    SystemSettings, StockStatus
)
from app.scraper import scraper
from app.scheduler import product_scheduler

logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title=settings.app_name,
    docs_url="/api/docs" if settings.debug else None,
    redoc_url=None,
)

# Setup templates and static files
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ============================================================================
# Middleware & Dependencies
# ============================================================================

SESSION_COOKIE = "costco_tracker_session"


def get_session_token(request: Request) -> Optional[str]:
    """Get session token from cookie."""
    return request.cookies.get(SESSION_COOKIE)


def require_auth(request: Request, db: Session = Depends(get_db)) -> bool:
    """Dependency that requires authentication."""
    # Check IP allowlist
    client_ip = request.client.host if request.client else "127.0.0.1"
    if not check_ip_allowed(client_ip):
        raise HTTPException(status_code=403, detail="IP not allowed")

    # Check if password is set
    pw_setting = db.query(SystemSettings).filter(
        SystemSettings.key == "site_password_hash"
    ).first()

    if not pw_setting or not pw_setting.value:
        # No password set - allow access (first run)
        return True

    # Validate session
    token = get_session_token(request)
    if not token or not SessionManager.validate_session(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required"
        )

    return True


def is_authenticated(request: Request, db: Session) -> bool:
    """Check if user is authenticated (non-raising version)."""
    # Check if password is set
    pw_setting = db.query(SystemSettings).filter(
        SystemSettings.key == "site_password_hash"
    ).first()

    if not pw_setting or not pw_setting.value:
        # No password set - allow access (first run)
        return True

    # Validate session
    token = get_session_token(request)
    return token is not None and SessionManager.validate_session(token)


def is_setup_complete(db: Session) -> bool:
    """Check if initial setup has been completed."""
    setting = db.query(SystemSettings).filter(
        SystemSettings.key == "site_password_hash"
    ).first()
    return bool(setting and setting.value)


# ============================================================================
# Pydantic Models for API
# ============================================================================

class ProductCreate(BaseModel):
    url_or_item: str
    target_price: Optional[float] = None
    notify_back_in_stock: bool = True
    notify_price_drop: bool = True
    auto_add_to_basket: bool = False
    auto_add_quantity: int = 1
    auto_add_max_price: Optional[float] = None


class ProductUpdate(BaseModel):
    target_price: Optional[float] = None
    notify_back_in_stock: Optional[bool] = None
    notify_price_drop: Optional[bool] = None
    notify_target_price: Optional[bool] = None
    notify_lowest_ever: Optional[bool] = None
    auto_add_to_basket: Optional[bool] = None
    auto_add_quantity: Optional[int] = None
    auto_add_max_price: Optional[float] = None
    poll_interval_minutes: Optional[int] = None
    is_active: Optional[bool] = None


class SetupForm(BaseModel):
    site_password: str
    smtp_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_enabled: bool = False
    discord_webhook_url: str = ""


# ============================================================================
# Startup & Shutdown
# ============================================================================

@app.on_event("startup")
async def startup():
    """Initialize database and start scheduler."""
    init_db()

    # Load settings from database
    from app.config import load_settings_from_db
    try:
        load_settings_from_db()
        logger.info("Settings loaded from database")
    except Exception as e:
        logger.warning(f"Could not load settings from database: {e}")

    product_scheduler.start()
    logger.info("Application started")


@app.on_event("shutdown")
async def shutdown():
    """Cleanup on shutdown."""
    product_scheduler.stop()
    await scraper.close()
    logger.info("Application shutdown")


# ============================================================================
# Authentication Routes
# ============================================================================

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: Session = Depends(get_db)):
    """Login page."""
    if not is_setup_complete(db):
        return RedirectResponse("/setup", status_code=302)

    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": None,
    })


@app.post("/login")
async def login(
    request: Request,
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    """Handle login."""
    setting = db.query(SystemSettings).filter(
        SystemSettings.key == "site_password_hash"
    ).first()

    if not setting or not PasswordManager.verify_password(password, setting.value):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Invalid password",
        })

    # Create session
    token = SessionManager.create_session()
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=settings.session_timeout_minutes * 60,
    )
    return response


@app.get("/logout")
async def logout(request: Request):
    """Handle logout."""
    token = get_session_token(request)
    if token:
        SessionManager.destroy_session(token)

    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, db: Session = Depends(get_db)):
    """First-run setup page."""
    if is_setup_complete(db):
        return RedirectResponse("/", status_code=302)

    return templates.TemplateResponse("setup.html", {
        "request": request,
    })


@app.post("/setup")
async def setup(
    request: Request,
    site_password: str = Form(...),
    db: Session = Depends(get_db)
):
    """Handle initial setup."""
    if is_setup_complete(db):
        return RedirectResponse("/", status_code=302)

    # Hash and store password
    password_hash = PasswordManager.hash_password(site_password)
    setting = SystemSettings(key="site_password_hash", value=password_hash)
    db.add(setting)
    db.commit()

    # Auto-login
    token = SessionManager.create_session()
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=settings.session_timeout_minutes * 60,
    )
    return response


# ============================================================================
# Main Pages
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: Session = Depends(get_db)
):
    """Main dashboard."""
    if not is_setup_complete(db):
        return RedirectResponse("/setup", status_code=302)

    if not is_authenticated(request, db):
        return RedirectResponse("/login", status_code=302)

    products = db.query(Product).filter(
        Product.is_active == True
    ).order_by(Product.name).all()

    # Get scheduler status
    last_run = db.query(SchedulerStatus).order_by(
        SchedulerStatus.run_started_at.desc()
    ).first()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "products": products,
        "last_run": last_run,
        "next_run": product_scheduler.get_next_run(),
        "kill_switch": settings.kill_switch,
        "safe_mode": settings.safe_mode,
    })


@app.get("/product/{product_id}", response_class=HTMLResponse)
async def product_detail(
    request: Request,
    product_id: int,
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_auth)
):
    """Product detail page with history."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    # Get price history (last 90 days)
    cutoff = datetime.utcnow() - timedelta(days=90)
    price_history = db.query(PriceHistory).filter(
        PriceHistory.product_id == product_id,
        PriceHistory.recorded_at >= cutoff
    ).order_by(PriceHistory.recorded_at).all()

    # Get stock history
    stock_history = db.query(StockHistory).filter(
        StockHistory.product_id == product_id,
        StockHistory.recorded_at >= cutoff
    ).order_by(StockHistory.recorded_at.desc()).limit(50).all()

    # Get recent alerts
    alerts = db.query(Alert).filter(
        Alert.product_id == product_id
    ).order_by(Alert.sent_at.desc()).limit(20).all()

    return templates.TemplateResponse("product.html", {
        "request": request,
        "product": product,
        "price_history": price_history,
        "stock_history": stock_history,
        "alerts": alerts,
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_auth)
):
    """Settings page."""
    # Reload settings from database to ensure they're current
    from app.config import load_settings_from_db
    load_settings_from_db()

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "settings": settings,
    })


@app.get("/status", response_class=HTMLResponse)
async def status_page(
    request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_auth)
):
    """System status page."""
    runs = db.query(SchedulerStatus).order_by(
        SchedulerStatus.run_started_at.desc()
    ).limit(50).all()

    # Products with errors
    error_products = db.query(Product).filter(
        Product.consecutive_errors > 0
    ).all()

    return templates.TemplateResponse("status.html", {
        "request": request,
        "runs": runs,
        "error_products": error_products,
        "scheduler_running": product_scheduler.is_running,
        "next_run": product_scheduler.get_next_run(),
    })


# ============================================================================
# Product Management (HTMX endpoints)
# ============================================================================

@app.post("/products/add", response_class=HTMLResponse)
async def add_product(
    request: Request,
    url_or_item: str = Form(...),
    target_price: Optional[float] = Form(None),
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_auth)
):
    """Add a new product to track."""
    import re

    # Extract item number
    if url_or_item.startswith("http"):
        match = re.search(r'/p/(\d+)', url_or_item)
        item_number = match.group(1) if match else url_or_item
        url = url_or_item
    else:
        item_number = url_or_item.strip()
        url = f"{settings.costco_base_url}/p/{item_number}"

    # Check if already exists
    existing = db.query(Product).filter(
        Product.item_number == item_number
    ).first()

    if existing:
        existing.is_active = True
        db.commit()
        return templates.TemplateResponse("partials/product_row.html", {
            "request": request,
            "product": existing,
            "message": "Product reactivated",
        })

    # Fetch product info
    data = await scraper.fetch_product(item_number)

    # Create product
    product = Product(
        item_number=item_number,
        url=url,
        name=data.name,
        image_url=data.image_url,
        current_price=data.price,
        lowest_price=data.price,
        highest_price=data.price,
        stock_status=data.stock_status.value,
        target_price=target_price,
        checkout_discount=data.checkout_discount,
        checkout_discount_text=data.checkout_discount_text,
        last_checked_at=datetime.utcnow(),
    )

    if data.stock_status == StockStatus.IN_STOCK:
        product.last_in_stock_at = datetime.utcnow()

    db.add(product)
    db.commit()

    # Record initial history
    if data.price:
        db.add(PriceHistory(product_id=product.id, price=data.price))
    db.add(StockHistory(product_id=product.id, status=data.stock_status.value))
    db.commit()

    return templates.TemplateResponse("partials/product_row.html", {
        "request": request,
        "product": product,
    })


@app.post("/products/{product_id}/refresh", response_class=HTMLResponse)
async def refresh_product(
    request: Request,
    product_id: int,
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_auth)
):
    """Manually refresh a product."""
    await product_scheduler.poll_single_product(product_id)

    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404)

    return templates.TemplateResponse("partials/product_row.html", {
        "request": request,
        "product": product,
    })


@app.delete("/products/{product_id}")
async def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_auth)
):
    """Remove a product (soft delete)."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if product:
        product.is_active = False
        db.commit()
    return Response(status_code=200)


@app.post("/products/{product_id}/update")
async def update_product_form(
    request: Request,
    product_id: int,
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_auth)
):
    """Update product settings via form."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404)

    form_data = await request.form()

    # Update fields from form
    if "target_price" in form_data and form_data["target_price"]:
        product.target_price = float(form_data["target_price"])
    elif "target_price" in form_data:
        product.target_price = None

    if "poll_interval_minutes" in form_data and form_data["poll_interval_minutes"]:
        product.poll_interval_minutes = int(form_data["poll_interval_minutes"])
    elif "poll_interval_minutes" in form_data:
        product.poll_interval_minutes = None

    # Boolean checkboxes
    product.notify_back_in_stock = form_data.get("notify_back_in_stock") == "true"
    product.notify_price_drop = form_data.get("notify_price_drop") == "true"
    product.notify_target_price = form_data.get("notify_target_price") == "true"
    product.notify_lowest_ever = form_data.get("notify_lowest_ever") == "true"
    product.auto_add_to_basket = form_data.get("auto_add_to_basket") == "true"

    if "auto_add_quantity" in form_data and form_data["auto_add_quantity"]:
        product.auto_add_quantity = int(form_data["auto_add_quantity"])

    if "auto_add_max_price" in form_data and form_data["auto_add_max_price"]:
        product.auto_add_max_price = float(form_data["auto_add_max_price"])
    elif "auto_add_max_price" in form_data:
        product.auto_add_max_price = None

    db.commit()

    return RedirectResponse(f"/product/{product_id}?success=settings_saved", status_code=302)


@app.patch("/products/{product_id}")
async def update_product(
    product_id: int,
    data: ProductUpdate,
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_auth)
):
    """Update product settings via API."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404)

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(product, key, value)

    db.commit()
    return {"status": "ok"}


# ============================================================================
# REST API
# ============================================================================

@app.get("/api/products")
async def api_list_products(
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_auth)
):
    """List all tracked products."""
    products = db.query(Product).filter(Product.is_active == True).all()
    return [{
        "id": p.id,
        "item_number": p.item_number,
        "name": p.name,
        "url": p.url,
        "current_price": p.current_price,
        "lowest_price": p.lowest_price,
        "stock_status": p.stock_status,
        "last_checked": p.last_checked_at.isoformat() if p.last_checked_at else None,
    } for p in products]


@app.get("/api/products/{product_id}")
async def api_get_product(
    product_id: int,
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_auth)
):
    """Get product details."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404)

    return {
        "id": product.id,
        "item_number": product.item_number,
        "name": product.name,
        "url": product.url,
        "current_price": product.current_price,
        "previous_price": product.previous_price,
        "lowest_price": product.lowest_price,
        "highest_price": product.highest_price,
        "stock_status": product.stock_status,
        "target_price": product.target_price,
        "is_clearance": product.is_clearance_price,
        "last_checked": product.last_checked_at.isoformat() if product.last_checked_at else None,
        "last_in_stock": product.last_in_stock_at.isoformat() if product.last_in_stock_at else None,
    }


@app.get("/api/products/{product_id}/history")
async def api_product_history(
    product_id: int,
    days: int = Query(90, ge=1, le=365),
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_auth)
):
    """Get product price history."""
    cutoff = datetime.utcnow() - timedelta(days=days)

    price_history = db.query(PriceHistory).filter(
        PriceHistory.product_id == product_id,
        PriceHistory.recorded_at >= cutoff
    ).order_by(PriceHistory.recorded_at).all()

    return [{
        "price": h.price,
        "recorded_at": h.recorded_at.isoformat(),
    } for h in price_history]


@app.get("/api/export")
async def api_export(
    format: str = Query("json", pattern="^(json|csv)$"),
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_auth)
):
    """Export all product data."""
    products = db.query(Product).all()

    if format == "csv":
        import csv
        import io

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "item_number", "name", "current_price", "lowest_price",
            "highest_price", "stock_status", "target_price", "last_checked"
        ])

        for p in products:
            writer.writerow([
                p.item_number, p.name, p.current_price, p.lowest_price,
                p.highest_price, p.stock_status, p.target_price,
                p.last_checked_at.isoformat() if p.last_checked_at else ""
            ])

        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=costco_products.csv"}
        )

    else:
        return [{
            "item_number": p.item_number,
            "name": p.name,
            "url": p.url,
            "current_price": p.current_price,
            "lowest_price": p.lowest_price,
            "highest_price": p.highest_price,
            "stock_status": p.stock_status,
            "target_price": p.target_price,
            "last_checked": p.last_checked_at.isoformat() if p.last_checked_at else None,
        } for p in products]


@app.post("/api/scheduler/run")
async def api_run_scheduler(
    _auth: bool = Depends(require_auth)
):
    """Trigger manual scheduler run."""
    await product_scheduler.run_now()
    return {"status": "ok", "message": "Scheduler run triggered"}


@app.post("/api/kill-switch/{state}")
async def api_kill_switch(
    state: str,
    _auth: bool = Depends(require_auth)
):
    """Toggle kill switch."""
    settings.kill_switch = state.lower() == "on"
    return {"status": "ok", "kill_switch": settings.kill_switch}


# ============================================================================
# Settings Management API
# ============================================================================

@app.post("/api/settings/notifications")
async def save_notification_settings(
    request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_auth)
):
    """Save notification settings to database."""
    form_data = await request.form()

    # Save each setting to SystemSettings table
    settings_to_save = {
        "smtp_enabled": form_data.get("smtp_enabled") == "true",
        "smtp_host": form_data.get("smtp_host", ""),
        "smtp_port": form_data.get("smtp_port", "587"),
        "smtp_username": form_data.get("smtp_username", ""),
        "smtp_password": form_data.get("smtp_password", ""),
        "smtp_from_email": form_data.get("smtp_from_email", ""),
        "telegram_enabled": form_data.get("telegram_enabled") == "true",
        "telegram_bot_token": form_data.get("telegram_bot_token", ""),
        "telegram_chat_id": form_data.get("telegram_chat_id", ""),
        "discord_enabled": form_data.get("discord_enabled") == "true",
        "discord_webhook_url": form_data.get("discord_webhook_url", ""),
        "pushover_enabled": form_data.get("pushover_enabled") == "true",
        "pushover_app_token": form_data.get("pushover_app_token", ""),
        "pushover_user_key": form_data.get("pushover_user_key", ""),
    }

    for key, value in settings_to_save.items():
        setting = db.query(SystemSettings).filter(SystemSettings.key == key).first()
        if setting:
            setting.value = str(value)
        else:
            setting = SystemSettings(key=key, value=str(value))
            db.add(setting)

    db.commit()

    # Update runtime settings
    for key, value in settings_to_save.items():
        setattr(settings, key, value)

    return RedirectResponse("/settings?success=notifications", status_code=302)


@app.post("/api/settings/scheduler")
async def save_scheduler_settings(
    request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_auth)
):
    """Save scheduler settings to database."""
    form_data = await request.form()

    settings_to_save = {
        "default_poll_interval_minutes": int(form_data.get("default_poll_interval_minutes", 45)),
        "min_poll_interval_minutes": int(form_data.get("min_poll_interval_minutes", 15)),
        "max_poll_interval_minutes": int(form_data.get("max_poll_interval_minutes", 180)),
        "request_timeout_seconds": int(form_data.get("request_timeout_seconds", 30)),
        "safe_mode": form_data.get("safe_mode") == "true",
    }

    for key, value in settings_to_save.items():
        setting = db.query(SystemSettings).filter(SystemSettings.key == key).first()
        if setting:
            setting.value = str(value)
        else:
            setting = SystemSettings(key=key, value=str(value))
            db.add(setting)

    db.commit()

    # Update runtime settings
    for key, value in settings_to_save.items():
        setattr(settings, key, value)

    return RedirectResponse("/settings?success=scheduler", status_code=302)


@app.post("/api/settings/checkout")
async def save_checkout_settings(
    request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_auth)
):
    """Save assisted checkout settings to database."""
    form_data = await request.form()

    settings_to_save = {
        "auto_add_to_basket_enabled": form_data.get("auto_add_to_basket_enabled") == "true",
        "costco_email": form_data.get("costco_email", ""),
    }

    # Handle password if provided
    costco_password = form_data.get("costco_password", "").strip()
    if costco_password:
        from app.security import CredentialEncryption
        settings_to_save["costco_password_encrypted"] = CredentialEncryption.encrypt(costco_password)

    for key, value in settings_to_save.items():
        setting = db.query(SystemSettings).filter(SystemSettings.key == key).first()
        if setting:
            setting.value = str(value)
        else:
            setting = SystemSettings(key=key, value=str(value))
            db.add(setting)

    db.commit()

    # Update runtime settings
    for key, value in settings_to_save.items():
        setattr(settings, key, value)

    return RedirectResponse("/settings?success=checkout", status_code=302)


@app.post("/change-password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_auth)
):
    """Change site password."""
    setting = db.query(SystemSettings).filter(
        SystemSettings.key == "site_password_hash"
    ).first()

    if not setting or not PasswordManager.verify_password(current_password, setting.value):
        return templates.TemplateResponse("settings.html", {
            "request": request,
            "settings": settings,
            "error": "Current password is incorrect",
        })

    # Update password
    setting.value = PasswordManager.hash_password(new_password)
    db.commit()

    return RedirectResponse("/settings?success=password_changed", status_code=302)


@app.post("/api/test-notification/{channel}")
async def test_notification(
    channel: str,
    _auth: bool = Depends(require_auth)
):
    """Send a test notification to verify settings."""
    from app.notifications import notifications

    # Reload settings from DB to ensure we're using latest
    from app.config import load_settings_from_db
    load_settings_from_db()

    try:
        if channel == "email":
            result = await notifications.send_email(
                subject="Test Email from Costco Tracker",
                body="This is a test message to verify your email settings are working correctly.\n\nIf you received this, your SMTP configuration is correct!"
            )
        elif channel == "telegram":
            result = await notifications.send_telegram(
                "<b>Test Message from Costco Tracker</b>\n\nYour Telegram notifications are working correctly!"
            )
        elif channel == "discord":
            result = await notifications.send_discord(
                subject="Test Notification from Costco Tracker",
                body="Your Discord webhook is working correctly! You'll receive product alerts here."
            )
        elif channel == "pushover":
            result = await notifications.send_pushover(
                subject="Test from Costco Tracker",
                body="Your Pushover notifications are working correctly!"
            )
        else:
            return JSONResponse({"success": False, "error": "Invalid channel"}, status_code=400)

        if result.success:
            return JSONResponse({"success": True, "message": f"Test {channel} notification sent!"})
        else:
            return JSONResponse({"success": False, "error": result.error}, status_code=500)

    except Exception as e:
        logger.error(f"Test notification failed for {channel}: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
