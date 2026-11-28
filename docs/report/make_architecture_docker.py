import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
from pathlib import Path

out = Path(r"C:/Users/johnq/Documents/test/School/CSCE4355/Assignment_Instruction/Final_Project/CSCE4355-FinalProj/docs/report/figures/architecture_docker.png")
fig, ax = plt.subplots(figsize=(11.2, 7.6), dpi=200)
ax.set_xlim(0, 112); ax.set_ylim(0, 76); ax.axis('off')

NAVY='#1F3A5F'; BLUE='#2E6DA4'; LIGHT='#E8EEF7'; GREY='#5B6770'; GREEN='#2E7D5B'; ORANGE='#B35C00'

def box(x,y,w,h,title,lines,fc=LIGHT,ec=BLUE,lw=1.4,title_color=NAVY):
    p=FancyBboxPatch((x,y),w,h,boxstyle="round,pad=0.35,rounding_size=1.2",
                     linewidth=lw, edgecolor=ec, facecolor=fc)
    ax.add_patch(p)
    ax.text(x+w/2, y+h-1.8, title, ha='center', va='top', fontsize=11.5,
            fontweight='bold', color=title_color)
    if lines:
        ax.text(x+w/2, y+h-4.2, "\n".join(lines), ha='center', va='top',
                fontsize=8.9, color='#222222', linespacing=1.35)

def arrow(x1,y1,x2,y2,label=None, color=GREY, style='-|>', lw=1.5, rad=0.0):
    a=FancyArrowPatch((x1,y1),(x2,y2), arrowstyle=style, mutation_scale=13,
                      linewidth=lw, color=color, connectionstyle=f"arc3,rad={rad}")
    ax.add_patch(a)
    if label:
        ax.text((x1+x2)/2, (y1+y2)/2+1.3, label, ha='center', va='bottom',
                fontsize=8.2, color=color, backgroundcolor='white')

ax.text(56, 74, "Delivered Docker Compose stack: PostgreSQL 16 monitoring pipeline",
        ha='center', va='top', fontsize=16, fontweight='bold', color=NAVY)
ax.text(56, 71.2, "one command:  bash scripts/start_dashboard.sh   (docker compose up -d --build)",
        ha='center', va='top', fontsize=9.6, color=GREY)

box(3, 43, 20, 18, "Operator", ["Browser", "http://127.0.0.1:8501", "", "HTTP on loopback", "(no TLS)"], fc='#F7F9FC', ec=GREY)
box(31, 43, 27, 18, "dashboard  (Streamlit)", ["connects as monitor_reader", "SELECT on 12 monitor.v_*", "read-only views only", "Overview / Alerts / Locks /", "Performance / Help"], fc='#F4FAF6', ec=GREEN, title_color=GREEN)
box(66, 50, 24, 13, "notifier", ["connects as monitor_collector", "polls monitor.alert_log every 30 s;", "stamps notified_at and", "resolve_notified_at"], fc='#FFF8EE', ec=ORANGE, title_color=ORANGE)
box(27, 8, 63, 30, "postgres  (PostgreSQL 16.16 + pg_cron 1.6)", [
    "shop schema: 6 tables (customers, orders, products, inventory, order_items, payments)",
    "monitor schema: 11 tables, 12 views  |  history + alert_log",
    "roles: monitor_collector owns monitor  |  monitor_reader: SELECT on v_* only",
    "pg_cron: capture_snapshot() every 60 s, purge 03:17, rollup hourly, cron cleanup 03:29",
    "capture_snapshot() -> history, then evaluate_alerts() -> extra -> more (6 rules)"],
    fc=LIGHT, ec=NAVY, lw=1.8)
box(3, 8, 20, 20, "demo  (profile)", ["docker compose", "run --rm demo", "", "lock_demo.py as", "shop_app holds a", "row lock for 120 s"], fc='#FBF2F2', ec='#A43A3A', title_color='#A43A3A')
box(93, 8, 16, 20, "volume", ["pgdata", "", "history survives", "container stop;", "deleted only by", "down -v"], fc='#F7F7F7', ec=GREY, title_color=GREY)

arrow(23.5, 47, 30.5, 47, "HTTP")
arrow(44.5, 43, 44.5, 38.5, "SELECT v_*", color=GREEN)
arrow(78, 50, 78, 38.5, "poll / stamp", color=ORANGE, style='<|-|>')
arrow(23.5, 12, 26.5, 12, "SQL")
ax.text(56, 4.8, "Docker named volume  pgdata  (PostgreSQL data directory)", ha='center', va='center', fontsize=8.8, color=GREY)
ax.text(56, 1.3, "Host ports bind to 127.0.0.1 only (HTTP, no TLS). Dashboard and notifier each connect directly to postgres:5432; there is no dashboard\u2192notifier path.",
        ha='center', va='bottom', fontsize=8.4, color=GREY)

fig.savefig(out, bbox_inches='tight', facecolor='white')
print(out, out.stat().st_size)
