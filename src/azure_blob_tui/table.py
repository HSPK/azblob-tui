from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    min_width: int
    preferred_width: int
    flex: int = 0
    align: str = "left"
    hide_priority: int = 0
    max_width: int | None = None


@dataclass(frozen=True)
class FittedColumn:
    column: Column
    width: int


def _total_width(columns: list[FittedColumn], gap: int) -> int:
    return sum(item.width for item in columns) + gap * max(len(columns) - 1, 0)


def fit_columns(
    columns: list[Column],
    available_width: int,
    gap: int = 2,
) -> list[FittedColumn]:
    if not columns or available_width <= 0:
        return []

    visible = list(columns)
    while len(visible) > 1:
        minimum = (
            sum(column.min_width for column in visible)
            + gap * (len(visible) - 1)
        )
        if minimum <= available_width:
            break
        removable = max(
            range(1, len(visible)),
            key=lambda index: (
                visible[index].hide_priority,
                index,
            ),
        )
        visible.pop(removable)

    fitted = [
        FittedColumn(
            column=column,
            width=min(
                max(column.min_width, column.preferred_width),
                column.max_width
                if column.max_width is not None
                else max(column.min_width, column.preferred_width),
            ),
        )
        for column in visible
    ]
    overflow = _total_width(fitted, gap) - available_width
    if overflow > 0:
        for index in reversed(range(len(fitted))):
            item = fitted[index]
            shrink = min(
                overflow,
                item.width - item.column.min_width,
            )
            if shrink:
                fitted[index] = FittedColumn(
                    item.column,
                    item.width - shrink,
                )
                overflow -= shrink
            if overflow <= 0:
                break

    extra = available_width - _total_width(fitted, gap)
    while extra > 0:
        flex_indexes = [
            index
            for index, item in enumerate(fitted)
            if item.column.flex
            and (
                item.column.max_width is None
                or item.width < item.column.max_width
            )
        ]
        if not flex_indexes:
            break
        flex_total = sum(fitted[index].column.flex for index in flex_indexes)
        allocated = 0
        for index in flex_indexes:
            item = fitted[index]
            share = max(extra * item.column.flex // flex_total, 1)
            if item.column.max_width is not None:
                share = min(
                    share,
                    item.column.max_width - item.width,
                )
            share = min(share, extra - allocated)
            if share <= 0:
                continue
            fitted[index] = FittedColumn(item.column, item.width + share)
            allocated += share
            if allocated >= extra:
                break
        if allocated == 0:
            break
        extra -= allocated
    return fitted


def format_cell(value: object, width: int, align: str = "left") -> str:
    text = str(value)
    if len(text) > width:
        text = text[:width] if width <= 3 else f"{text[: width - 3]}..."
    if align == "right":
        return text.rjust(width)
    return text.ljust(width)


def render_header(columns: list[FittedColumn], gap: int = 2) -> str:
    return (" " * gap).join(
        format_cell(item.column.label, item.width)
        for item in columns
    )


def render_row(
    columns: list[FittedColumn],
    values: dict[str, object],
    gap: int = 2,
) -> str:
    return (" " * gap).join(
        format_cell(
            values.get(item.column.key, ""),
            item.width,
            item.column.align,
        )
        for item in columns
    )
