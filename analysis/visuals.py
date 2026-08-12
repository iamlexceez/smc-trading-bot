"""
Visual chart rendering for SMC Trading Bot.
Uses matplotlib to generate professional charts with SMC markings.
"""

import matplotlib
matplotlib.use('Agg') # Use non-interactive backend for headless servers
import matplotlib.pyplot as plt
import pandas as pd
import numpy as np
import io
import os

def render_smc_chart(df: pd.DataFrame, symbol: str, structure, zones, signal=None) -> bytes:
    """Render a candlestick chart with SMC annotations and return as bytes."""
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(12, 7))
    
    # Plot candlesticks (simplified)
    width = 0.6
    width2 = 0.05
    
    up = df[df.close >= df.open]
    down = df[df.close < df.open]
    
    # Plotting up candles
    ax.bar(up.index, up.close - up.open, width, bottom=up.open, color='#26a69a')
    ax.bar(up.index, up.high - up.close, width2, bottom=up.close, color='#26a69a')
    ax.bar(up.index, up.low - up.open, width2, bottom=up.open, color='#26a69a')
    
    # Plotting down candles
    ax.bar(down.index, down.close - down.open, width, bottom=down.open, color='#ef5350')
    ax.bar(down.index, down.high - down.open, width2, bottom=down.open, color='#ef5350')
    ax.bar(down.index, down.low - down.close, width2, bottom=down.close, color='#ef5350')
    
    # Plot Zones
    for zone in zones:
        color = 'green' if zone.zone_type.value == 'demand' else 'red'
        ax.axhspan(zone.bottom, zone.top, alpha=0.2, color=color, label=zone.zone_type.value.capitalize())
    
    # Plot Structure Events (BOS/CHoCH)
    if structure.last_event.value != 'none':
        event_text = structure.last_event.value.replace('_', ' ').upper()
        last_idx = df.index[-1]
        last_price = df.iloc[-1]['close']
        ax.annotate(event_text, xy=(last_idx, last_price), xytext=(last_idx - 10, last_price + (last_price * 0.001)),
                    arrowprops=dict(facecolor='white', shrink=0.05, width=1, headwidth=5),
                    color='white', fontweight='bold')

    # Plot Signal Entry/SL/TP if provided
    if signal:
        ax.axhline(signal.entry_price, color='blue', linestyle='--', alpha=0.6, label='Entry')
        ax.axhline(signal.stop_loss, color='orange', linestyle='--', alpha=0.6, label='SL')
        ax.axhline(signal.take_profit, color='lime', linestyle='--', alpha=0.6, label='TP')

    ax.set_title(f"SMC Analysis: {symbol}", fontsize=15, color='white')
    ax.set_ylabel("Price")
    ax.grid(True, alpha=0.1)
    
    # Save to buffer
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100)
    buf.seek(0)
    plt.close(fig)
    return buf.read()
