import plotly.express as px

def daily_line_chart(df, y_col, title, unit, color):
    """
    Generates a daily line chart with point markers and 12-hour time formatting on the x-axis.
    """
    fig = px.line(
        df,
        x="timestamp",
        y=y_col,
        title=f"{title} ({unit})",
        labels={"timestamp": "Time", y_col: f"{title} ({unit})"},
        markers=True,  # Displays small dots for every single data point
    )

    # Customize line style and marker size
    fig.update_traces(
        line=dict(color=color, width=2),
        marker=dict(size=5, color=color),  # Adjust 'size' to change dot size
        hovertemplate=f"<b>Time</b>: %{{x|%I:%M %p}}<br><b>{title}</b>: %{{y}} {unit}<extra></extra>"
    )

    # Format the X-axis for 12-hour time (%I:%M %p -> 02:30 PM)
    fig.update_xaxes(
        dtick="3600000",  # Gridline ticks every 1 hour (in milliseconds)
        tickformat="%I:%M %p",  # 12-hour format with AM/PM
        showgrid=True,
        gridcolor="#262730",
    )

    fig.update_yaxes(showgrid=True, gridcolor="#262730")

    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=20, r=20, t=40, b=20),
        height=300,
    )

    return fig