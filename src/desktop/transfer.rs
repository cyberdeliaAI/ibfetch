//! The transfer window: counter, meter, speed, and the lists of what happened.
use super::{
    text::{base, num, plural, size, span},
    theme::{self, Palette},
};
use eframe::egui::{
    self, Color32, FontId, Margin, Rect, RichText, Sense, Stroke, TextWrapMode, Vec2, pos2, vec2,
};
use serde_json::Value;

pub enum Action {
    Stop,
    OpenFolder,
    Retry,
}

fn count(value: &Value) -> u64 {
    value.as_u64().unwrap_or(0)
}
fn lines(value: &Value) -> &[Value] {
    value.as_array().map_or(&[], Vec::as_slice)
}
pub fn running(transfer: &Value) -> bool {
    matches!(
        transfer["state"].as_str(),
        Some("ready" | "planning" | "running")
    )
}
/// The line under the meter, as on the browser page.
pub fn stats(transfer: &Value) -> String {
    let bytes = size(transfer["bytes"].as_f64().unwrap_or(0.));
    let elapsed = span(transfer["elapsed"].as_f64().unwrap_or(0.));
    if running(transfer) {
        let speed = transfer["speed"].as_f64().unwrap_or(0.);
        let eta = transfer["eta"].as_f64().unwrap_or(0.);
        let mut text = bytes;
        if speed > 0. {
            text += &format!(" at {}/s", size(speed));
        }
        if eta > 0. {
            text += &format!(", about {} left", span(eta));
        }
        text
    } else if transfer["state"] == "done" {
        format!("Done in {elapsed}, {bytes} saved")
    } else {
        format!("Stopped after {elapsed}. Try again to carry on.")
    }
}

fn lamp(ui: &mut egui::Ui, p: &Palette, state: &str, busy: bool) {
    let (rect, _) = ui.allocate_exact_size(Vec2::splat(8.), Sense::hover());
    let (color, glow) = if busy {
        // A slow blink between full and 45% opacity, 1.6 s per cycle.
        let phase = ui.input(|i| i.time) / 1.6 * std::f64::consts::TAU;
        let opacity = 1. - 0.55 * (0.5 - 0.5 * phase.cos()) as f32;
        (
            Color32::from_rgb(0xff, 0x54, 0x46).gamma_multiply(opacity),
            true,
        )
    } else if state == "done" {
        (p.window_ink, true)
    } else {
        (Color32::from_rgb(0x4a, 0x1a, 0x16), false)
    };
    if glow {
        ui.painter()
            .circle_filled(rect.center(), 7., color.gamma_multiply(0.22));
    }
    ui.painter().circle_filled(rect.center(), 4., color);
}
fn counter(ui: &mut egui::Ui, p: &Palette, done: u64, total: u64) {
    let width = total.to_string().len();
    let big = RichText::new(format!("{done:0width$}"))
        .font(FontId::new(46., theme::rounded()))
        .variation("wght", 300.)
        .extra_letter_spacing(0.9)
        .color(p.window_ink);
    let small = RichText::new(format!(" / {total}"))
        .font(FontId::new(20., theme::rounded()))
        .color(p.window_dim);
    let big = egui::WidgetText::from(big).into_galley(
        ui,
        Some(TextWrapMode::Extend),
        f32::INFINITY,
        egui::TextStyle::Body,
    );
    let small = egui::WidgetText::from(small).into_galley(
        ui,
        Some(TextWrapMode::Extend),
        f32::INFINITY,
        egui::TextStyle::Body,
    );
    let height = (big.size().y - 8.).max(small.size().y);
    let (rect, _) = ui.allocate_exact_size(vec2(ui.available_width(), height), Sense::hover());
    let painter = ui.painter_at(rect.expand(2.));
    let top = rect.top() - 5.;
    // Both parts share a baseline: the small text sits on the digits' foot line.
    let small_pos = pos2(
        rect.left() + big.size().x + 2.,
        top + big.size().y * 0.80 - small.size().y * 0.78,
    );
    painter.galley(pos2(rect.left(), top), big, p.window_ink);
    painter.galley(small_pos, small, p.window_dim);
}
fn meter(ui: &mut egui::Ui, p: &Palette, fraction: f32) {
    let (rect, _) = ui.allocate_exact_size(vec2(ui.available_width(), 12.), Sense::hover());
    let shown =
        ui.ctx()
            .animate_value_with_time(ui.scope_id().with("meter"), fraction.clamp(0., 1.), 0.4);
    let lit = rect.left() + rect.width() * shown;
    let painter = ui.painter_at(rect);
    let mut x = rect.left();
    // Seven-point segments with a two-point gap; the lit part may end inside one.
    while x < rect.right() {
        let segment = Rect::from_min_max(
            pos2(x, rect.top()),
            pos2((x + 7.).min(rect.right()), rect.bottom()),
        );
        painter.rect_filled(segment, 0., p.window_track);
        if lit > x {
            let part =
                Rect::from_min_max(segment.min, pos2(segment.right().min(lit), rect.bottom()));
            painter.rect_filled(part, 0., p.window_ink);
        }
        x += 9.;
    }
}
fn mini(ui: &mut egui::Ui, p: &Palette, received: f64, expected: f64) {
    let (rect, _) = ui.allocate_exact_size(vec2(ui.available_width(), 3.), Sense::hover());
    let painter = ui.painter_at(rect);
    painter.rect_filled(rect, 2., p.line);
    let (from, width) = if expected > 0. {
        (0., (received / expected).clamp(0., 1.) as f32)
    } else {
        // Size not known yet: a block sliding back and forth every 1.4 s.
        let phase = (ui.input(|i| i.time) / 1.4) % 2.;
        let t = if phase > 1. { 2. - phase } else { phase } as f32;
        (0.65 * t * t * (3. - 2. * t), 0.35)
    };
    let bar = Rect::from_min_size(
        pos2(rect.left() + rect.width() * from, rect.top()),
        vec2(rect.width() * width, rect.height()),
    );
    painter.rect_filled(bar, 2., p.accent_fill);
}
fn details(ui: &mut egui::Ui, p: &Palette, id: &str, summary: String, rows: &[Value]) {
    if rows.is_empty() {
        return;
    }
    egui::CollapsingHeader::new(theme::muted(p, summary, 12.))
        .id_salt(id)
        .show(ui, |ui| {
            egui::ScrollArea::vertical()
                .id_salt(id)
                .max_height(160.)
                .show(ui, |ui| {
                    for row in rows {
                        let text = format!(
                            "{}: {}",
                            row["name"].as_str().unwrap_or_default(),
                            row["note"].as_str().unwrap_or_default()
                        );
                        theme::help(ui, p, &text);
                    }
                });
        });
}

pub fn show(ui: &mut egui::Ui, p: &Palette, transfer: &Value, can_retry: bool) -> Option<Action> {
    let state = transfer["state"].as_str().unwrap_or_default();
    let busy = running(transfer);
    let (total, done, failed) = (
        count(&transfer["total"]),
        count(&transfer["done"]),
        count(&transfer["fail"]),
    );
    let mut action = None;
    egui::Frame::new()
        .fill(p.window)
        .corner_radius(12)
        .stroke(Stroke::new(1., Color32::BLACK))
        .inner_margin(Margin {
            left: 16,
            right: 16,
            top: 14,
            bottom: 12,
        })
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.spacing_mut().item_spacing = vec2(8., 0.);
            ui.horizontal(|ui| {
                lamp(ui, p, state, busy);
                let label = match state {
                    "planning" | "ready" => "Preparing download",
                    "running" => "Downloading",
                    "done" => "Finished",
                    _ => "Stopped",
                };
                ui.label(RichText::new(label).size(12.).color(p.window_dim));
            });
            ui.add_space(6.);
            counter(ui, p, done, total);
            ui.add_space(8.);
            meter(
                ui,
                p,
                if total > 0 {
                    done as f32 / total as f32
                } else {
                    0.
                },
            );
            ui.add_space(9.);
            ui.add(
                egui::Label::new(
                    RichText::new(stats(transfer))
                        .size(13.)
                        .color(p.window_stats),
                )
                .wrap(),
            );
            ui.add_space(4.);
            ui.horizontal_wrapped(|ui| {
                ui.spacing_mut().item_spacing.x = 0.;
                for (i, (key, word)) in [("ok", "saved"), ("skip", "skipped"), ("fail", "failed")]
                    .into_iter()
                    .enumerate()
                {
                    if i > 0 {
                        ui.add_space(14.);
                    }
                    ui.label(
                        RichText::new(num(count(&transfer[key])))
                            .size(12.)
                            .variation("wght", 500.)
                            .color(p.tally),
                    );
                    ui.label(
                        RichText::new(format!(" {word}"))
                            .size(12.)
                            .color(p.window_dim),
                    );
                }
            });
        });
    ui.add_space(4.);
    ui.horizontal_wrapped(|ui| {
        if busy && ui.button("Stop").clicked() {
            action = Some(Action::Stop);
        }
        if ui.button("Open folder").clicked() {
            action = Some(Action::OpenFolder);
        }
        let stopped = state == "stopped";
        if !busy
            && can_retry
            && (failed > 0 || stopped)
            && ui
                .button(if stopped { "Carry on" } else { "Retry failed" })
                .clicked()
        {
            action = Some(Action::Retry);
        }
    });
    if busy {
        ui.add_space(6.);
        for file in lines(&transfer["active"]) {
            let name = base(file["name"].as_str().unwrap_or_default());
            ui.add(egui::Label::new(theme::muted(p, name, 12.)).truncate());
            ui.add_space(-3.);
            mini(
                ui,
                p,
                file["received"].as_f64().unwrap_or(0.),
                file["expected"].as_f64().unwrap_or(0.),
            );
        }
    }
    ui.add_space(4.);
    let recent = lines(&transfer["recent"]);
    for row in recent.iter().take(if busy { 6 } else { 4 }) {
        let name = row["name"].as_str().unwrap_or_default();
        let note = row["note"].as_str().unwrap_or_default();
        let (label, color) = match row["state"].as_str() {
            Some("ok") => ("Saved", p.ink),
            Some("skip") => ("Skipped", p.ink),
            _ => ("Failed", p.red),
        };
        // A fixed column for the outcome, then the file name.
        let (line, response) =
            ui.allocate_exact_size(vec2(ui.available_width(), 18.), Sense::hover());
        let label = theme::fit(ui, RichText::new(label).size(12.).color(color), 86.);
        theme::paint(ui, line, false, label);
        let file = Rect::from_min_max(pos2(line.left() + 86., line.top()), line.max);
        let file_name = theme::fit(ui, theme::muted(p, base(name), 12.), file.width());
        theme::paint(ui, file, false, file_name);
        response.on_hover_text(if note.is_empty() {
            name.to_owned()
        } else {
            format!("{name}: {note}")
        });
        ui.add_space(-5.);
    }
    ui.add_space(8.);
    details(
        ui,
        p,
        "transfer-failed",
        format!("{} failed, show which", num(failed)),
        lines(&transfer["failures"]),
    );
    details(
        ui,
        p,
        "transfer-existing",
        format!(
            "{}: details",
            plural(count(&transfer["existing_count"]), "existing file")
        ),
        lines(&transfer["existing"]),
    );
    details(
        ui,
        p,
        "transfer-notes",
        format!(
            "{} saved with a note: details",
            num(count(&transfer["notes_count"]))
        ),
        lines(&transfer["notes"]),
    );
    action
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn stats_line_reports_speed_remaining_time_and_the_outcome() {
        let mut t = json!({"state":"running","bytes":262144000u64,"speed":11534336.0,"eta":55.2,"elapsed":23.0});
        assert_eq!(stats(&t), "250 MB at 11 MB/s, about 55 s left");
        t["speed"] = json!(0.0);
        t["eta"] = Value::Null;
        assert_eq!(stats(&t), "250 MB");
        t["state"] = json!("planning");
        assert!(running(&t));
        t["state"] = json!("done");
        t["elapsed"] = json!(125.0);
        assert!(!running(&t));
        assert_eq!(stats(&t), "Done in 2 min, 250 MB saved");
        t["state"] = json!("stopped");
        assert_eq!(stats(&t), "Stopped after 2 min. Try again to carry on.");
    }
}
