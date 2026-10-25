import os
import re
import hashlib
import threading
from html import unescape
from urllib.parse import urlparse, urljoin

import requests

from kivy.app import App
from kivy.clock import mainthread
from kivy.logger import Logger
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.image import AsyncImage
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget

# ========== Configuration ==========
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
SKIP_EXTS = (".svg",)  # Kivy cannot render SVG


def _default_download_dir():
    try:
        from android.storage import primary_external_storage_path  # type: ignore
        base = primary_external_storage_path()
        if not base:
            raise RuntimeError("no primary storage")
        return os.path.join(base, "Download", "WebImages")
    except Exception:
        return os.path.join(os.path.expanduser("~"), "Downloads", "WebImages")


DOWNLOAD_DIR = _default_download_dir()


def _request_android_permissions():
    try:
        from android.permissions import request_permissions, Permission  # type: ignore
        request_permissions([
            Permission.INTERNET,
            Permission.WRITE_EXTERNAL_STORAGE,
            Permission.READ_EXTERNAL_STORAGE,
        ])
    except Exception:
        pass  # not on Android


# ========== Web page scraper ==========
def _pick_best_from_srcset(srcset):
    """Return the highest-resolution URL from a srcset attribute."""
    candidates = []
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        url = bits[0]
        descriptor = bits[1] if len(bits) > 1 else "1x"
        try:
            score = float(descriptor.rstrip("wx"))
        except ValueError:
            score = 1.0
        candidates.append((score, url))
    best = max(candidates, default=(0, None))
    return best[1]


def scrape_images(page_url, limit=50):
    """Fetch page_url and return (list_of_items, error)."""
    if not page_url.lower().startswith(("http://", "https://")):
        page_url = "https://" + page_url
    try:
        resp = requests.get(
            page_url,
            timeout=30,
            headers={
                "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        resp.raise_for_status()
    except Exception as e:
        Logger.warning(f"scrape fetch failed: {e}")
        return [], f"Fetch failed: {e}"

    base = resp.url or page_url
    candidates = []

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")

        for a in soup.find_all("a", href=True):
            href = urljoin(base, a["href"])
            if urlparse(href).path.lower().endswith(IMAGE_EXTS):
                candidates.append((href, "orig"))

        for meta in soup.find_all("meta"):
            prop = (meta.get("property") or meta.get("name") or "").lower()
            if prop in ("og:image", "og:image:secure_url", "twitter:image"):
                if meta.get("content"):
                    candidates.append((urljoin(base, meta["content"]), "orig"))

        for link in soup.find_all("link", rel="image_src", href=True):
            candidates.append((urljoin(base, link["href"]), "orig"))

        for tag in soup.find_all("img"):
            for attr in ("data-full", "data-original", "data-src", "src"):
                if tag.get(attr):
                    candidates.append((urljoin(base, tag[attr]), "thumb"))
                    break
            if tag.get("srcset"):
                best = _pick_best_from_srcset(tag["srcset"])
                if best:
                    candidates.append((urljoin(base, best), "orig"))

    except ImportError:
        for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', resp.text, re.I):
            candidates.append((urljoin(base, unescape(m.group(1))), "thumb"))

    seen, items = set(), []
    for u, kind in candidates:
        u = u.strip()
        if not u or u.startswith("data:"):
            continue
        if urlparse(u).path.lower().endswith(SKIP_EXTS):
            continue
        if u in seen:
            continue
        seen.add(u)
        items.append({
            "original": u,
            "thumbnail": u,
            "title": os.path.basename(urlparse(u).path) or "image",
            "source": urlparse(u).netloc,
            "kind": kind,
        })
        if len(items) >= limit:
            break

    return items, None


# ========== Download ==========
MAX_BYTES = 25 * 1024 * 1024  # 25 MB


def _safe_filename(title, img_url):
    safe_title = "".join(c for c in title if c.isalnum() or c in " -_")[:60].strip()
    if not safe_title:
        safe_title = "image"
    ext = os.path.splitext(urlparse(img_url).path)[1].lower()
    if ext not in IMAGE_EXTS:
        ext = ".jpg"
    digest = hashlib.sha1(img_url.encode("utf-8")).hexdigest()[:8]
    return f"{safe_title}_{digest}{ext}"


def download_image(item, cancel_event=None):
    img_url = item.get("original")
    if not img_url:
        return "no URL"
    filename = _safe_filename(item.get("title", "image"), img_url)
    try:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    except Exception as e:
        return f"dir fail: {e}"
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    if os.path.exists(filepath):
        return "exists"

    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36",
        "Accept": "image/*,*/*;q=0.8",
        "Referer": "https://" + urlparse(img_url).netloc + "/",
    }
    tmp = filepath + ".part"
    try:
        with requests.get(img_url, stream=True, timeout=30, headers=headers) as resp:
            resp.raise_for_status()
            ctype = resp.headers.get("Content-Type", "").lower()
            if ctype and not ctype.startswith("image/"):
                return f"skip (content-type {ctype})"
            length = int(resp.headers.get("Content-Length") or 0)
            if length > MAX_BYTES:
                return f"skip (too big: {length} bytes)"
            written = 0
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(8192):
                    if cancel_event and cancel_event.is_set():
                        raise RuntimeError("cancelled")
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > MAX_BYTES:
                        raise RuntimeError("exceeded size cap")
                    f.write(chunk)
            if written < 64:
                raise RuntimeError("suspiciously small payload")
        os.replace(tmp, filepath)
        return "ok"
    except Exception as e:
        Logger.warning(f"Download failed for {img_url}: {e}")
        return f"fail: {e}"
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


# ========== Result row widget ==========
class ResultRow(BoxLayout):
    def __init__(self, item, on_toggle=None, **kwargs):
        super().__init__(
            orientation="horizontal", size_hint_y=None,
            height=dp(120), padding=dp(4), spacing=dp(6), **kwargs,
        )
        self.item = item
        self._on_toggle = on_toggle

        self.checkbox = CheckBox(size_hint=(None, 1), width=dp(40))
        self.checkbox.bind(active=self._checkbox_changed)
        self.add_widget(self.checkbox)

        thumb_url = item.get("thumbnail") or item.get("original") or ""
        img = AsyncImage(
            source=thumb_url, size_hint=(None, 1),
            width=dp(120), allow_stretch=True, keep_ratio=True,
        )
        self.add_widget(img)

        info = BoxLayout(orientation="vertical")

        title_lbl = Label(
            text=item.get("title", "N/A"),
            size_hint_y=None, height=dp(50),
            halign="left", valign="top",
            shorten=True, shorten_from="right",
            text_size=(dp(200), None),
        )
        title_lbl.bind(
            width=lambda inst, w: setattr(inst, "text_size", (w, None))
        )

        source_lbl = Label(
            text=item.get("source", "N/A"),
            size_hint_y=None, height=dp(30),
            halign="left", valign="middle",
            color=(0.7, 0.7, 0.7, 1),
            font_size="12sp",
        )

        info.add_widget(title_lbl)
        info.add_widget(source_lbl)
        info.add_widget(Widget())
        self.add_widget(info)

    def _checkbox_changed(self, _inst, _value):
        if self._on_toggle:
            self._on_toggle()

    @property
    def selected(self):
        return self.checkbox.active

    def set_selected(self, value):
        """Set without firing the callback twice (Kivy re-triggers on change)."""
        if self.checkbox.active != bool(value):
            self.checkbox.active = bool(value)


# ========== Main app ==========
class ImageScraperApp(App):
    def build(self):
        _request_android_permissions()

        root = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(8))

        # URL bar
        url_bar = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(6))
        self.url_input = TextInput(
            hint_text="Paste webpage URL (https://...)",
            multiline=False, size_hint_x=0.7,
        )
        self.scrape_btn = Button(text="Load Page", size_hint_x=0.3)
        self.scrape_btn.bind(on_release=self.on_scrape)
        url_bar.add_widget(self.url_input)
        url_bar.add_widget(self.scrape_btn)
        root.add_widget(url_bar)

        # Status
        self.status = Label(text="", size_hint_y=None, height=dp(30))
        root.add_widget(self.status)

        # Scrollable results
        self.scroll = ScrollView()
        self.results_box = BoxLayout(
            orientation="vertical", size_hint_y=None, spacing=dp(4),
        )
        self.results_box.bind(minimum_height=self.results_box.setter("height"))
        self.scroll.add_widget(self.results_box)
        root.add_widget(self.scroll)

        # Selection bar: [Select All] [N selected]
        self.selection_bar = BoxLayout(
            size_hint_y=None, height=dp(44), spacing=dp(6),
        )
        self.select_all_btn = Button(
            text="Select All", size_hint_x=0.5, disabled=True,
        )
        self.select_all_btn.bind(on_release=self._toggle_select_all)
        self.selection_bar.add_widget(self.select_all_btn)

        self.selection_count_lbl = Label(
            text="0 selected", size_hint_x=0.5,
            halign="left", valign="middle",
        )
        self.selection_count_lbl.bind(
            size=lambda inst, s: setattr(inst, "text_size", s)
        )
        self.selection_bar.add_widget(self.selection_count_lbl)
        root.add_widget(self.selection_bar)

        # Bottom: download
        self.download_btn = Button(
            text="Download Selected", size_hint_y=None, height=dp(50),
            disabled=True,
        )
        self.download_btn.bind(on_release=self.on_download)
        root.add_widget(self.download_btn)

        self.rows = []
        self._busy = False
        self._cancel_event = None
        return root

    # ----- helpers -----
    def _set_busy(self, busy, status_text=None):
        self._busy = busy
        self.scrape_btn.disabled = busy
        self.select_all_btn.disabled = busy or not self.rows
        self.download_btn.disabled = busy or not self._any_selected()
        if status_text is not None:
            self.status.text = status_text

    def _any_selected(self):
        return any(r.selected for r in self.rows)

    def _populate(self, items, err, empty_msg):
        if err:
            self.status.text = f"Error: {err}"
            self._refresh_selection_ui()
            return
        if not items:
            self.status.text = empty_msg
            self._refresh_selection_ui()
            return
        self.status.text = f"Found {len(items)} images."
        for item in items:
            row = ResultRow(item, on_toggle=self._on_row_toggled)
            self.results_box.add_widget(row)
            self.rows.append(row)
        self._refresh_selection_ui()

    # ----- selection -----
    def _on_row_toggled(self):
        self._refresh_selection_ui()

    def _refresh_selection_ui(self):
        """Recompute count, button labels, and enable/disable states."""
        total = len(self.rows)
        selected = sum(1 for r in self.rows if r.selected)

        self.selection_count_lbl.text = f"{selected} selected"

        if total == 0:
            self.select_all_btn.text = "Select All"
            self.select_all_btn.disabled = True
        else:
            self.select_all_btn.disabled = self._busy
            if selected == total:
                self.select_all_btn.text = "Deselect All"
            else:
                self.select_all_btn.text = "Select All"

        if selected > 0:
            self.download_btn.text = f"Download Selected ({selected})"
        else:
            self.download_btn.text = "Download Selected"
        self.download_btn.disabled = self._busy or selected == 0

    def _toggle_select_all(self, *args):
        if self._busy or not self.rows:
            return
        total = len(self.rows)
        selected = sum(1 for r in self.rows if r.selected)
        target = not (selected == total)   # True = select all, False = deselect
        for row in self.rows:
            row.set_selected(target)
        self._refresh_selection_ui()

    # ----- page scrape -----
    def on_scrape(self, *args):
        if self._busy:
            return
        url = self.url_input.text.strip()
        if not url:
            self.status.text = "Please enter a URL."
            return
        self._set_busy(True, f"Loading {url}...")
        self.results_box.clear_widgets()
        self.rows = []
        self._refresh_selection_ui()
        threading.Thread(
            target=self._scrape_worker, args=(url,), daemon=True,
        ).start()

    def _scrape_worker(self, url):
        items, err = scrape_images(url, limit=50)
        self._on_scrape_done(items, err)

    @mainthread
    def _on_scrape_done(self, items, err):
        self._set_busy(False)
        self._populate(items, err, "No images found on that page.")

    # ----- download -----
    def on_download(self, *args):
        if self._busy:
            return
        selected = [r.item for r in self.rows if r.selected]
        if not selected:
            self.status.text = "Nothing selected."
            return
        self._set_busy(True, f"Downloading 0/{len(selected)}...")
        self._cancel_event = threading.Event()
        threading.Thread(
            target=self._download_worker,
            args=(selected, self._cancel_event),
            daemon=True,
        ).start()

    def _download_worker(self, selected, cancel_event):
        total = len(selected)
        ok = 0
        failed = 0
        for i, item in enumerate(selected, 1):
            if cancel_event.is_set():
                break
            res = download_image(item, cancel_event=cancel_event)
            if res in ("ok", "exists"):
                ok += 1
            elif res.startswith("fail"):
                failed += 1
            self._update_download_progress(i, total, ok, failed)
        self._on_download_done(ok, failed, total, cancel_event.is_set())

    @mainthread
    def _update_download_progress(self, done, total, ok, failed):
        self.status.text = (
            f"Downloading {done}/{total}  (ok: {ok}, failed: {failed})"
        )

    @mainthread
    def _on_download_done(self, ok, failed, total, cancelled):
        prefix = "Cancelled" if cancelled else "Done"
        self.status.text = f"{prefix}: {ok}/{total} saved, {failed} failed."
        self._set_busy(False)
        self._refresh_selection_ui()


if __name__ == "__main__":
    ImageScraperApp().run()