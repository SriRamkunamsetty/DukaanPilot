from .read_tools import (
    check_inventory,
    detect_sales_anomalies,
    explain_sales_change,
    forecast_demand,
    get_sales_summary,
    get_shop_profile,
    get_stock_alerts,
    get_top_products,
    get_upcoming_festivals,
    list_reorder_drafts,
)
from .write_tools import (
    cancel_reorder_draft,
    create_reorder_drafts,
    log_restock,
    log_sale,
    request_reorder_approval,
    undo_sale,
    update_preference,
)

ALL_TOOLS = [
    get_sales_summary, get_top_products, check_inventory, get_stock_alerts, forecast_demand,
    explain_sales_change, detect_sales_anomalies, get_upcoming_festivals, list_reorder_drafts,
    get_shop_profile, log_sale, undo_sale, log_restock, create_reorder_drafts,
    request_reorder_approval, cancel_reorder_draft, update_preference,
]

__all__ = ["ALL_TOOLS"]
