//! Release notes: the same bounded Markdown subset as the bundled browser page.
use super::theme::{self, Palette};
use eframe::egui::{self, RichText, Sense, vec2};

const LIMIT: usize = 50_000;
const URL_LIMIT: usize = 8192;

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Span {
    pub text: String,
    pub strong: bool,
    pub emphasis: bool,
    pub code: bool,
    pub link: Option<String>,
}
#[derive(Clone, Debug, PartialEq)]
pub enum Block {
    Heading(Vec<Span>),
    Paragraph(Vec<Span>),
    List(bool, Vec<Vec<Span>>),
    Code(String),
    Rule,
    Table(Vec<Vec<Span>>, Vec<Vec<Vec<Span>>>),
}
struct Budget {
    inline: usize,
    cells: usize,
}

/// Only plain HTTPS links without credentials open from release notes.
fn safe_url(value: &str) -> Option<String> {
    if value.len() > URL_LIMIT || value.chars().any(|c| c.is_whitespace() || c.is_control()) {
        return None;
    }
    let url = reqwest::Url::parse(value).ok()?;
    (url.scheme() == "https" && url.username().is_empty() && url.password().is_none())
        .then(|| url.to_string())
}
fn link_end(text: &str, start: usize) -> Option<usize> {
    let mut depth = 0usize;
    for (i, c) in text[start..].char_indices() {
        if i > URL_LIMIT {
            break;
        }
        match c {
            '(' => depth += 1,
            ')' if depth == 0 => return Some(start + i),
            ')' => depth -= 1,
            _ => {}
        }
    }
    None
}
fn word(c: Option<char>) -> bool {
    c.is_some_and(|c| c.is_alphanumeric() || matches!(c, '*' | '_'))
}
fn plain(text: &str, style: &Span, out: &mut Vec<Span>) {
    if text.is_empty() {
        return;
    }
    if let Some(last) = out.last_mut()
        && (last.strong, last.emphasis, last.code) == (style.strong, style.emphasis, style.code)
        && last.link == style.link
    {
        last.text.push_str(text);
        return;
    }
    out.push(Span {
        text: text.into(),
        ..style.clone()
    });
}
fn inline(
    text: &str,
    style: &Span,
    budget: &mut Budget,
    depth: u8,
    links: bool,
    out: &mut Vec<Span>,
) {
    let (mut copied, mut at) = (0, 0);
    while budget.inline > 0 {
        // The next backtick run, link, bare HTTPS address or emphasis marker.
        let Some((start, marker)) = text[at..].char_indices().find_map(|(i, c)| {
            let rest = &text[at + i..];
            let run = |limit: usize| rest.chars().take_while(|&x| x == c).take(limit).count();
            match c {
                '`' => Some((at + i, run(usize::MAX))),
                '!' if rest.starts_with("![") => Some((at + i, 2)),
                '[' => Some((at + i, 1)),
                'h' if rest.starts_with("https://") => Some((at + i, 8)),
                '*' | '_' => Some((at + i, run(2))),
                _ => None,
            }
        }) else {
            break;
        };
        budget.inline -= 1;
        let body = start + marker;
        at = body;
        let first = text[start..].chars().next().unwrap_or_default();
        let before = text[..start].chars().next_back();
        let mut take = |end: usize, spans: Vec<Span>, out: &mut Vec<Span>| {
            plain(&text[copied..start], style, out);
            out.extend(spans);
            copied = end;
            at = end;
        };
        match first {
            '`' => {
                let fence = &text[start..body];
                if let Some(end) = text[body..].find(fence).map(|i| body + i)
                    && end > body
                {
                    let code = Span {
                        text: text[body..end].into(),
                        code: true,
                        ..style.clone()
                    };
                    take(end + marker, vec![code], out);
                }
            }
            '[' | '!' => {
                let Some(label_end) = text[body..].find("](").map(|i| body + i) else {
                    continue;
                };
                let Some(end) = link_end(text, label_end + 2) else {
                    continue;
                };
                let label = &text[body..label_end];
                let url = safe_url(&text[label_end + 2..end]);
                let mut spans = Vec::new();
                match url {
                    Some(url)
                        if first == '[' && links && !label.is_empty() && !label.contains('[') =>
                    {
                        let linked = Span {
                            link: Some(url),
                            ..style.clone()
                        };
                        inline(label, &linked, budget, depth + 1, false, &mut spans);
                    }
                    _ => plain(&text[start..=end], style, &mut spans),
                }
                take(end + 1, spans, out);
            }
            'h' => {
                if !links || before.is_some_and(|c| !c.is_whitespace() && c != '(') {
                    continue;
                }
                let end = text[body..]
                    .find(|c: char| c.is_whitespace() || "<>\"'`".contains(c))
                    .map_or(text.len(), |i| body + i);
                let mut value = text[start..end].trim_end_matches(['.', ',', ';', ':', '!', '?']);
                let mut balance = value.chars().fold(0i32, |n, c| match c {
                    '(' => n + 1,
                    ')' => n - 1,
                    _ => n,
                });
                while balance < 0 && value.ends_with(')') {
                    value = &value[..value.len() - 1];
                    balance += 1;
                }
                match safe_url(value) {
                    Some(url) => {
                        let linked = Span {
                            text: value.into(),
                            link: Some(url),
                            ..style.clone()
                        };
                        take(start + value.len(), vec![linked], out);
                    }
                    None => {
                        let mut spans = Vec::new();
                        plain(&text[start..end], style, &mut spans);
                        take(end, spans, out);
                    }
                }
            }
            _ => {
                let single = marker == 1;
                let opens = depth < 2
                    && text[body..]
                        .chars()
                        .next()
                        .is_some_and(|c| !c.is_whitespace())
                    && !(single && word(before));
                let Some(end) = text[body..].find(&text[start..body]).map(|i| body + i) else {
                    continue;
                };
                let closes = end > body
                    && text[..end]
                        .chars()
                        .next_back()
                        .is_some_and(|c| !c.is_whitespace())
                    && !(single && word(text[end + marker..].chars().next()));
                if opens && closes {
                    let inner = Span {
                        strong: style.strong || !single,
                        emphasis: style.emphasis || single,
                        ..style.clone()
                    };
                    let mut spans = Vec::new();
                    inline(
                        &text[body..end],
                        &inner,
                        budget,
                        depth + 1,
                        links,
                        &mut spans,
                    );
                    take(end + marker, spans, out);
                }
            }
        }
    }
    plain(&text[copied..], style, out);
}
fn spans(text: &str, budget: &mut Budget) -> Vec<Span> {
    let mut out = Vec::new();
    inline(text, &Span::default(), budget, 0, true, &mut out);
    out
}
fn indent(line: &str) -> Option<&str> {
    let rest = line.trim_start_matches(' ');
    (line.len() - rest.len() <= 3).then_some(rest)
}
fn fence(line: &str) -> Option<(char, usize)> {
    let rest = indent(line)?;
    let c = rest.chars().next().filter(|c| matches!(c, '`' | '~'))?;
    let n = rest.chars().take_while(|&x| x == c).count();
    (n >= 3).then_some((c, n))
}
fn heading(line: &str) -> Option<&str> {
    let rest = indent(line)?;
    let level = rest.bytes().take_while(|&b| b == b'#').count();
    let label = rest[level..].strip_prefix([' ', '\t'])?.trim();
    if !(1..=6).contains(&level) || label.is_empty() {
        return None;
    }
    // Drop a closing run of hashes that is set apart by a space.
    let open = label.trim_end_matches('#');
    Some(
        if open.len() < label.len() && open.ends_with(char::is_whitespace) {
            open.trim_end()
        } else {
            label
        },
    )
}
fn rule(line: &str) -> bool {
    let Some(rest) = indent(line) else {
        return false;
    };
    let marks: Vec<char> = rest.chars().filter(|c| !matches!(c, ' ' | '\t')).collect();
    marks.len() >= 3
        && matches!(marks[0], '-' | '*' | '_')
        && marks.iter().all(|&c| c == marks[0])
        && !rest.starts_with(['\t'])
}
fn table_line(line: &str) -> bool {
    let line = line.trim();
    line.len() >= 2 && line.starts_with('|') && line.ends_with('|')
}
fn cells(line: &str) -> Vec<&str> {
    let line = line.trim();
    let line = line.strip_prefix('|').unwrap_or(line);
    let line = line.strip_suffix('|').unwrap_or(line);
    line.split('|').map(str::trim).collect()
}
fn separator(cell: &str) -> bool {
    let dashes = cell.strip_prefix(':').unwrap_or(cell);
    let dashes = dashes.strip_suffix(':').unwrap_or(dashes);
    dashes.len() >= 2 && dashes.bytes().all(|b| b == b'-')
}
fn item(line: &str) -> Option<(bool, &str)> {
    let rest = line.trim_start();
    let (ordered, after) = if let Some(after) = rest.strip_prefix(['-', '*', '+']) {
        (false, after)
    } else {
        let digits = rest.bytes().take_while(u8::is_ascii_digit).count();
        if !(1..=9).contains(&digits) {
            return None;
        }
        (true, rest[digits..].strip_prefix(['.', ')'])?)
    };
    let text = after.trim_start();
    (text.len() < after.len()).then_some((ordered, text))
}

pub fn parse(markdown: &str) -> Vec<Block> {
    let mut end = markdown.len().min(LIMIT);
    while !markdown.is_char_boundary(end) {
        end -= 1;
    }
    let source = markdown[..end]
        .replace("\r\n", "\n")
        .replace('\r', "\n")
        .replace('\0', "\u{fffd}");
    if source.trim().is_empty() {
        return vec![];
    }
    let lines: Vec<&str> = source.split('\n').collect();
    let mut budget = Budget {
        inline: 1500,
        cells: 2000,
    };
    let mut blocks = Vec::new();
    let mut paragraph: Vec<&str> = Vec::new();
    let mut list: Option<(bool, Vec<String>)> = None;
    fn close(
        blocks: &mut Vec<Block>,
        paragraph: &mut Vec<&str>,
        list: &mut Option<(bool, Vec<String>)>,
        budget: &mut Budget,
        which: (bool, bool),
    ) {
        if which.0 && !paragraph.is_empty() {
            blocks.push(Block::Paragraph(spans(&paragraph.join(" "), budget)));
            paragraph.clear();
        }
        if which.1
            && let Some((ordered, items)) = list.take()
        {
            let items = items.iter().map(|item| spans(item, budget)).collect();
            blocks.push(Block::List(ordered, items));
        }
    }
    let mut i = 0;
    while i < lines.len() {
        let line = lines[i];
        i += 1;
        if let Some((mark, length)) = fence(line) {
            close(
                &mut blocks,
                &mut paragraph,
                &mut list,
                &mut budget,
                (true, true),
            );
            let mut code = Vec::new();
            while i < lines.len() {
                let closing = fence(lines[i]).is_some_and(|(c, n)| {
                    c == mark && n >= length && lines[i].trim().chars().all(|x| x == c)
                });
                i += 1;
                if closing {
                    break;
                }
                code.push(lines[i - 1]);
            }
            blocks.push(Block::Code(code.join("\n")));
        } else if line.trim().is_empty() {
            close(
                &mut blocks,
                &mut paragraph,
                &mut list,
                &mut budget,
                (true, true),
            );
        } else if let Some(label) = heading(line) {
            close(
                &mut blocks,
                &mut paragraph,
                &mut list,
                &mut budget,
                (true, true),
            );
            blocks.push(Block::Heading(spans(label, &mut budget)));
        } else if rule(line) {
            close(
                &mut blocks,
                &mut paragraph,
                &mut list,
                &mut budget,
                (true, true),
            );
            blocks.push(Block::Rule);
        } else if table_line(line)
            && lines.get(i).is_some_and(|next| table_line(next))
            && let head = cells(line)
            && head.len() <= 20
            && head.len() <= budget.cells
            && cells(lines[i]).len() == head.len()
            && cells(lines[i]).iter().all(|cell| separator(cell))
        {
            close(
                &mut blocks,
                &mut paragraph,
                &mut list,
                &mut budget,
                (true, true),
            );
            budget.cells -= head.len();
            i += 1;
            let mut rows = Vec::new();
            while i < lines.len() && table_line(lines[i]) {
                let row = cells(lines[i]);
                if row.len() != head.len() || head.len() > budget.cells {
                    break;
                }
                budget.cells -= head.len();
                rows.push(row.iter().map(|cell| spans(cell, &mut budget)).collect());
                i += 1;
            }
            let head = head.iter().map(|cell| spans(cell, &mut budget)).collect();
            blocks.push(Block::Table(head, rows));
        } else if let Some((ordered, text)) = item(line) {
            close(
                &mut blocks,
                &mut paragraph,
                &mut list,
                &mut budget,
                (true, false),
            );
            if list.as_ref().is_some_and(|(kind, _)| *kind != ordered) {
                close(
                    &mut blocks,
                    &mut paragraph,
                    &mut list,
                    &mut budget,
                    (false, true),
                );
            }
            list.get_or_insert((ordered, vec![])).1.push(text.into());
        } else if let Some((_, items)) = &mut list
            && line.starts_with(char::is_whitespace)
            && let Some(last) = items.last_mut()
        {
            last.push(' ');
            last.push_str(line.trim());
        } else {
            close(
                &mut blocks,
                &mut paragraph,
                &mut list,
                &mut budget,
                (false, true),
            );
            paragraph.push(line.trim());
        }
    }
    close(
        &mut blocks,
        &mut paragraph,
        &mut list,
        &mut budget,
        (true, true),
    );
    blocks
}

fn styled(p: &Palette, span: &Span, size: f32, heading: bool) -> RichText {
    let mut text = RichText::new(&span.text).size(size);
    if span.strong || heading {
        text = text.variation("wght", 600.).color(p.ink);
    }
    if span.emphasis {
        text = text.italics();
    }
    if span.code {
        text = text.code().size(size - 1.);
    }
    if span.link.is_some() {
        text = text.color(p.accent);
    }
    text
}
fn line(ui: &mut egui::Ui, p: &Palette, spans: &[Span], size: f32, heading: bool) {
    ui.horizontal_wrapped(|ui| {
        ui.spacing_mut().item_spacing.x = 0.;
        for span in spans {
            let text = styled(p, span, size, heading);
            match &span.link {
                Some(url) => ui.hyperlink_to(text, url),
                None => ui.add(egui::Label::new(text).wrap()),
            };
        }
    });
}
/// A table cell keeps its text on one line; the table scrolls sideways instead.
fn cell(ui: &mut egui::Ui, p: &Palette, spans: &[Span], heading: bool) {
    ui.horizontal(|ui| {
        ui.spacing_mut().item_spacing.x = 0.;
        for span in spans {
            let text = styled(p, span, 14., heading);
            match &span.link {
                Some(url) => ui.hyperlink_to(text, url),
                None => ui.add(egui::Label::new(text).extend()),
            };
        }
    });
}
pub fn show(ui: &mut egui::Ui, p: &Palette, blocks: &[Block]) {
    ui.spacing_mut().item_spacing.y = 4.;
    ui.spacing_mut().interact_size.y = 0.;
    for (index, block) in blocks.iter().enumerate() {
        match block {
            Block::Heading(spans) => {
                if index > 0 {
                    ui.add_space(8.);
                }
                line(ui, p, spans, 15., true);
            }
            Block::Paragraph(spans) => line(ui, p, spans, 14., false),
            Block::List(ordered, items) => {
                for (n, item) in items.iter().enumerate() {
                    ui.horizontal_top(|ui| {
                        ui.spacing_mut().item_spacing.x = 0.;
                        let mark = if *ordered {
                            format!("{}.", n + 1)
                        } else {
                            "•".into()
                        };
                        let (cell, _) = ui.allocate_exact_size(vec2(22., 20.), Sense::hover());
                        let mark = theme::fit(ui, theme::muted(p, mark, 14.), 22.);
                        theme::paint(ui, cell.shrink2(vec2(4., 0.)), false, mark);
                        ui.vertical(|ui| line(ui, p, item, 14., false));
                    });
                }
            }
            Block::Code(code) => {
                egui::Frame::new()
                    .fill(p.accent_soft)
                    .corner_radius(6)
                    .inner_margin(8)
                    .show(ui, |ui| {
                        ui.add(egui::Label::new(RichText::new(code).monospace().size(13.)).wrap());
                    });
            }
            Block::Rule => {
                ui.separator();
            }
            Block::Table(head, rows) => {
                egui::ScrollArea::horizontal()
                    .id_salt(("notes-table", index))
                    .show(ui, |ui| {
                        egui::Grid::new(("notes-grid", index))
                            .striped(true)
                            .spacing([14., 6.])
                            .show(ui, |ui| {
                                for spans in head {
                                    cell(ui, p, spans, true);
                                }
                                ui.end_row();
                                for row in rows {
                                    for spans in row {
                                        cell(ui, p, spans, false);
                                    }
                                    ui.end_row();
                                }
                            });
                    });
            }
        }
        ui.add_space(4.);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(spans: &[Span]) -> String {
        spans.iter().map(|span| span.text.as_str()).collect()
    }

    #[test]
    fn blocks_follow_the_browser_renderer() {
        let blocks = parse(
            "## Added ##\r\n\r\nFirst line\nsecond **bold** and `code`.\n\n- one\n- two\n  continued\n1. first\n\n---\n\n```sh\ncargo run\n```\n\n| A | B |\n|---|:--:|\n| 1 | 2 |\n",
        );
        assert_eq!(blocks.len(), 7);
        assert!(matches!(&blocks[0], Block::Heading(spans) if text(spans) == "Added"));
        let Block::Paragraph(spans) = &blocks[1] else {
            panic!("paragraph")
        };
        assert_eq!(text(spans), "First line second bold and code.");
        assert!(spans.iter().any(|span| span.strong && span.text == "bold"));
        assert!(spans.iter().any(|span| span.code && span.text == "code"));
        assert!(
            matches!(&blocks[2], Block::List(false, items) if items.len() == 2 && text(&items[1]) == "two continued")
        );
        assert!(matches!(&blocks[3], Block::List(true, items) if items.len() == 1));
        assert_eq!(blocks[4], Block::Rule);
        assert_eq!(blocks[5], Block::Code("cargo run".into()));
        assert!(
            matches!(&blocks[6], Block::Table(head, rows) if head.len() == 2 && rows.len() == 1 && text(&rows[0][1]) == "2")
        );
        assert!(parse("  \n").is_empty());
    }

    #[test]
    fn only_https_links_without_credentials_open() {
        let Block::Paragraph(spans) = &parse(
            "See [the notes](https://example.com/a_(b)) or https://example.com/x). Not [this](http://example.com), [that](javascript:alert(1)), [user](https://me:pw@example.com) or ![img](https://example.com/i.png).",
        )[0] else {
            panic!("paragraph")
        };
        let links: Vec<_> = spans
            .iter()
            .filter_map(|span| span.link.as_deref().map(|url| (span.text.as_str(), url)))
            .collect();
        assert_eq!(
            links,
            [
                ("the notes", "https://example.com/a_(b)"),
                ("https://example.com/x", "https://example.com/x")
            ]
        );
        assert!(text(spans).contains("[this](http://example.com)"));
        assert!(text(spans).contains("![img](https://example.com/i.png)"));
    }

    #[test]
    fn emphasis_needs_word_boundaries_and_input_is_bounded() {
        let Block::Paragraph(spans) = &parse("snake_case_name stays, *this* is _emphasis_.")[0]
        else {
            panic!("paragraph")
        };
        let marked: Vec<_> = spans
            .iter()
            .filter(|span| span.emphasis)
            .map(|span| span.text.as_str())
            .collect();
        assert_eq!(marked, ["this", "emphasis"]);
        assert!(text(spans).starts_with("snake_case_name stays"));
        let long = "é".repeat(LIMIT);
        let Block::Paragraph(spans) = &parse(&long)[0] else {
            panic!("paragraph")
        };
        assert_eq!(text(spans).len(), LIMIT);
        let Block::Paragraph(spans) = &parse(&"*a* ".repeat(4000))[0] else {
            panic!("paragraph")
        };
        assert_eq!(spans.iter().filter(|span| span.emphasis).count(), 1500);
    }
}
