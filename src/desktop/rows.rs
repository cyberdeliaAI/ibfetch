//! The hand-drawn rows: letters, artists, albums with their track lists, and the download list.
use super::{
    Queued, TRACK,
    text::{clock, num},
    theme::{self, Palette, fit, paint},
};
use eframe::egui::{self, CursorIcon, Rangef, Rect, RichText, Sense, Stroke, pos2, vec2};
use ibfetch::{catalog::Library, model::Album};

pub(super) fn letter_button(ui: &mut egui::Ui, p: &Palette, label: &str, on: bool) -> bool {
    let size = vec2(if label.len() > 1 { 32. } else { 25. }, 21.);
    let (rect, response) = ui.allocate_exact_size(size, Sense::click());
    if on {
        ui.painter().rect_filled(rect, 4., p.shell_accent);
    } else if response.hovered() {
        ui.painter().rect_filled(rect, 4., p.shell_hover);
    }
    let text = if on {
        theme::semibold(label).size(12.).color(p.shell_on_accent)
    } else {
        RichText::new(label).size(12.).color(p.shell_muted)
    };
    let galley = fit(ui, text, 60.);
    ui.painter()
        .galley(rect.center() - galley.size() / 2., galley, p.shell_muted);
    response
        .widget_info(|| egui::WidgetInfo::selected(egui::accesskit::Role::Button, true, on, label));
    response.on_hover_cursor(CursorIcon::PointingHand).clicked()
}
pub(super) fn artist_row(
    ui: &mut egui::Ui,
    p: &Palette,
    name: &str,
    albums: u64,
    on: bool,
    bold: bool,
) -> bool {
    let (rect, response) = ui.allocate_exact_size(vec2(ui.available_width(), 28.), Sense::click());
    let inner = rect.shrink2(vec2(6., 0.));
    if on {
        ui.painter().rect_filled(inner, 5., p.shell_active);
    } else if response.hovered() {
        ui.painter().rect_filled(inner, 5., p.shell_hover);
    }
    let text = inner.shrink2(vec2(8., 0.));
    let count = fit(ui, RichText::new(num(albums)).color(p.shell_muted), 90.);
    let label = if bold {
        theme::semibold(name)
    } else {
        RichText::new(name)
    };
    let label = fit(
        ui,
        label.color(p.shell_ink),
        text.width() - count.size().x - 8.,
    );
    paint(ui, text, true, count);
    paint(ui, text, false, label);
    response
        .widget_info(|| egui::WidgetInfo::selected(egui::accesskit::Role::Button, true, on, name));
    response.clicked()
}

/// The result grid: `34 | 1fr | 1.6fr | 50 | 54 | 86 | 66` with ten-point gaps,
/// and only the first four columns in a narrow window.
pub(super) struct Columns {
    check: Rangef,
    cells: [Option<Rangef>; 6],
}
impl Columns {
    pub(super) fn new(left: f32, width: f32) -> Self {
        let gap = 10.;
        let wide = width - 8. - 16. - 34. - 50. - 54. - 86. - 66. - 6. * gap;
        let (pad, check, fixed, flexible, shares) = if wide >= 260. {
            (8., 34., vec![50., 54., 86., 66.], wide, (1., 1.6))
        } else {
            let narrow = width - 4. - 10. - 30. - 44. - 3. * gap;
            (4., 30., vec![44.], narrow.max(60.), (1., 1.3))
        };
        let mut x = left + pad;
        let mut next = |size: f32| {
            let range = Rangef::new(x, x + size);
            x += size + gap;
            range
        };
        let check = next(check);
        let mut cells = [None; 6];
        cells[0] = Some(next(flexible * shares.0 / (shares.0 + shares.1)));
        cells[1] = Some(next(flexible * shares.1 / (shares.0 + shares.1)));
        for (i, size) in fixed.into_iter().enumerate() {
            cells[2 + i] = Some(next(size));
        }
        Self { check, cells }
    }
    pub(super) fn check(&self, row: Rect) -> Rect {
        Rect::from_x_y_ranges(self.check, row.y_range())
    }
    pub(super) fn cells(&self, row: Rect) -> [Option<Rect>; 6] {
        self.cells
            .map(|cell| cell.map(|x| Rect::from_x_y_ranges(x, row.y_range())))
    }
}
#[derive(Clone, Copy)]
pub(super) struct RowState {
    pub(super) checked: bool,
    pub(super) queued: bool,
    pub(super) open: bool,
}
pub(super) enum RowAction {
    Select(bool),
    Expand,
}
pub(super) fn album_row(
    ui: &mut egui::Ui,
    p: &Palette,
    columns: &Columns,
    row: Rect,
    album: &Album,
    state: RowState,
) -> Option<RowAction> {
    let id = ui.scope_id().with(("album", album.id));
    let response = ui.interact(row, id, Sense::click());
    response.widget_info(|| {
        egui::WidgetInfo::selected(
            egui::accesskit::Role::Button,
            true,
            state.checked,
            format!("{}, {}", album.artist, album.title),
        )
    });
    let painter = ui.painter();
    if state.checked {
        painter.rect_filled(row, 0., p.accent_soft);
        let bar = Rect::from_min_size(row.min, vec2(3., row.height()));
        painter.rect_filled(bar, 0., p.accent);
    } else if ui.rect_contains_pointer(row) {
        painter.rect_filled(row, 0., p.panel);
    }
    painter.hline(
        row.x_range(),
        row.bottom(),
        Stroke::new(1., theme::mix(p.bg, p.line, 0.55)),
    );
    let cells = columns.cells(row);
    let muted = |text: String| RichText::new(text).color(p.muted);
    let mut elided = false;
    let mut cell = |ui: &egui::Ui, index: usize, right: bool, text: RichText| {
        if let Some(rect) = cells[index] {
            let galley = fit(ui, text, rect.width());
            elided |= galley.elided;
            paint(ui, rect, right, galley);
        }
    };
    cell(ui, 0, false, RichText::new(&album.artist).color(p.ink));
    let year = if album.year > 0 {
        album.year.to_string()
    } else {
        String::new()
    };
    cell(ui, 2, true, muted(year));
    cell(ui, 3, true, muted(album.tracks.len().to_string()));
    cell(ui, 4, false, RichText::new(&album.formats).color(p.ink));
    cell(ui, 5, true, muted(clock(album.length)));

    let mut action = None;
    if let Some(area) = cells[1] {
        // The arrow opens the track list; a dot marks albums already in the download list.
        let arrow = Rect::from_min_size(area.min, vec2(20., area.height()));
        let toggle = ui
            .interact(arrow, id.with("tracks"), Sense::click())
            .on_hover_cursor(CursorIcon::PointingHand);
        toggle.widget_info(|| {
            egui::WidgetInfo::selected(
                egui::accesskit::Role::Button,
                true,
                state.open,
                format!("Show the tracks of {}", album.title),
            )
        });
        let turn = ui
            .ctx()
            .animate_bool_with_time(id.with("turn"), state.open, 0.15);
        let centre = arrow.center();
        let angle = turn * std::f32::consts::FRAC_PI_2;
        let points = [vec2(-2.5, -4.), vec2(3.5, 0.), vec2(-2.5, 4.)].map(|v| {
            centre
                + vec2(
                    v.x * angle.cos() - v.y * angle.sin(),
                    v.x * angle.sin() + v.y * angle.cos(),
                )
        });
        ui.painter().add(egui::Shape::convex_polygon(
            points.to_vec(),
            if toggle.hovered() { p.ink } else { p.muted },
            Stroke::NONE,
        ));
        if toggle.clicked() {
            action = Some(RowAction::Expand);
        }
        let mut x = arrow.right() + 4.;
        if state.queued {
            ui.painter()
                .circle_filled(pos2(x + 3.5, area.center().y), 3.5, p.accent);
            x += 14.;
        }
        let disc = (album.disc > 0).then(|| {
            fit(
                ui,
                RichText::new(format!("disc {}", album.disc))
                    .size(12.)
                    .color(p.muted),
                80.,
            )
        });
        let reserved = disc.as_ref().map_or(0., |galley| galley.size().x + 6.);
        let title = fit(
            ui,
            RichText::new(&album.title).color(p.ink),
            (area.right() - x - reserved).max(20.),
        );
        elided |= title.elided;
        let end = x + title.size().x;
        paint(
            ui,
            Rect::from_x_y_ranges(x..=area.right(), area.y_range()),
            false,
            title,
        );
        if let Some(disc) = disc {
            let at = Rect::from_x_y_ranges(end + 6. ..=area.right(), area.y_range());
            paint(ui, at, false, disc);
        }
    }
    let mut checked = state.checked;
    let tick = ui.put(
        columns.check(row),
        egui::Checkbox::without_text(&mut checked),
    );
    if tick.changed() {
        action = Some(RowAction::Select(checked));
    } else if response.clicked() {
        action = Some(RowAction::Select(!state.checked));
    }
    if elided {
        response.on_hover_text(format!("{} · {}", album.artist, album.title));
    }
    action
}
pub(super) fn track_list(
    ui: &mut egui::Ui,
    p: &Palette,
    area: Rect,
    library: &Library,
    album: &Album,
) {
    let painter = ui.painter_at(area);
    painter.rect_filled(area, 0., p.panel);
    painter.hline(area.x_range(), area.bottom(), Stroke::new(1., p.line));
    let left = area.left() + 66f32.min(area.width() * 0.12);
    let right = area.right() - 16.;
    for (i, &id) in album.tracks.iter().enumerate() {
        let top = area.top() + 4. + TRACK * i as f32;
        let line = Rect::from_min_max(pos2(left, top), pos2(right, top + TRACK));
        if !ui.is_rect_visible(line) {
            continue;
        }
        let muted = |text: String| RichText::new(text).color(p.muted);
        let number = library.track_number(id);
        let number = if number > 0 {
            number.to_string()
        } else {
            String::new()
        };
        paint(ui, line, false, fit(ui, muted(number), 28.));
        let length = fit(ui, muted(clock(library.duration(id))), 70.);
        let format = fit(ui, muted(library.format(id)), 70.);
        let after = line.right() - length.size().x - 10. - format.size().x - 10.;
        paint(
            ui,
            Rect::from_x_y_ranges(
                after + 10. ..=line.right() - length.size().x - 10.,
                line.y_range(),
            ),
            false,
            format,
        );
        paint(ui, line, true, length);
        let start = line.left() + 38.;
        let title = fit(
            ui,
            RichText::new(library.track_title(id)).color(p.ink),
            (after - start).max(20.),
        );
        let end = start + title.size().x;
        paint(
            ui,
            Rect::from_x_y_ranges(start..=after, line.y_range()),
            false,
            title,
        );
        let artist = library.track_artist(id);
        if !artist.is_empty() && artist != album.artist && after - end > 40. {
            let artist = fit(ui, muted(artist), after - end - 10.);
            paint(
                ui,
                Rect::from_x_y_ranges(end + 10. ..=after, line.y_range()),
                false,
                artist,
            );
        }
    }
}
pub(super) fn queue_row(ui: &mut egui::Ui, p: &Palette, item: &Queued, last: bool) -> bool {
    let (row, _) = ui.allocate_exact_size(vec2(ui.available_width(), 44.), Sense::hover());
    if !last {
        ui.painter()
            .hline(row.x_range(), row.bottom(), Stroke::new(1., p.line));
    }
    let remove = Rect::from_min_max(pos2(row.right() - 32., row.top()), row.max);
    let response = ui
        .interact(
            remove,
            ui.scope_id().with(("remove", item.id)),
            Sense::click(),
        )
        .on_hover_cursor(CursorIcon::PointingHand)
        .on_hover_text("Remove");
    response.widget_info(|| {
        egui::WidgetInfo::labeled(
            egui::accesskit::Role::Button,
            true,
            format!("Remove {}", item.title),
        )
    });
    let cross =
        RichText::new("×")
            .size(16.)
            .color(if response.hovered() { p.red } else { p.muted });
    let cross = fit(ui, cross, 30.);
    ui.painter()
        .galley(remove.center() - cross.size() / 2., cross, p.muted);
    let count = fit(
        ui,
        RichText::new(item.lengths.len().to_string())
            .size(13.)
            .color(p.muted),
        60.,
    );
    let text = Rect::from_min_max(
        pos2(row.left() + 10., row.top() + 5.),
        pos2(remove.left() - 8. - count.size().x, row.bottom() - 5.),
    );
    paint(
        ui,
        Rect::from_min_max(
            pos2(text.right(), row.top()),
            pos2(remove.left(), row.bottom()),
        ),
        true,
        count,
    );
    let (upper, lower) = text.split_top_bottom_at_fraction(0.54);
    let disc = (item.disc > 0).then(|| {
        fit(
            ui,
            RichText::new(format!("disc {}", item.disc))
                .size(12.)
                .color(p.muted),
            70.,
        )
    });
    let reserved = disc.as_ref().map_or(0., |galley| galley.size().x + 6.);
    let title = fit(
        ui,
        RichText::new(&item.title).color(p.ink),
        text.width() - reserved,
    );
    let end = text.left() + title.size().x;
    paint(ui, upper, false, title);
    if let Some(disc) = disc {
        paint(
            ui,
            Rect::from_x_y_ranges(end + 6. ..=text.right(), upper.y_range()),
            false,
            disc,
        );
    }
    let artist = if item.year > 0 {
        format!("{}, {}", item.artist, item.year)
    } else {
        item.artist.clone()
    };
    paint(
        ui,
        lower,
        false,
        fit(
            ui,
            RichText::new(artist).size(12.).color(p.muted),
            text.width(),
        ),
    );
    response.clicked()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn result_columns_fit_wide_and_narrow_panes() {
        let wide = Columns::new(0., 900.);
        assert!(wide.cells.iter().all(Option::is_some));
        assert!((wide.cells[5].unwrap().max - (900. - 16.)).abs() < 0.01);
        let narrow = Columns::new(0., 420.);
        assert!(narrow.cells[3].is_none() && narrow.cells[2].is_some());
        assert!((narrow.cells[2].unwrap().max - (420. - 10.)).abs() < 0.01);
    }
}
