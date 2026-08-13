from dataclasses import dataclass
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Comment


EMAIL_HTML_SANITIZER_VERSION = 1


@dataclass(frozen=True)
class SanitizedEmailBody:
    html: str
    text: str
    policy_version: int = EMAIL_HTML_SANITIZER_VERSION


class EmailHtmlSanitizer:
    """Strict, versioned sanitizer for externally supplied email markup."""

    ALLOWED_TAGS = {
        "a", "abbr", "b", "blockquote", "br", "code", "del", "div", "em",
        "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "ins", "li",
        "ol", "p", "pre", "s", "span", "strong", "sub", "sup", "table",
        "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
    }
    DROP_WITH_CONTENT = {
        "applet", "audio", "base", "button", "canvas", "embed", "form", "frame",
        "frameset", "iframe", "input", "link", "math", "meta", "noscript",
        "object", "option", "picture", "script", "select", "source", "style",
        "svg", "template", "textarea", "track", "video",
    }
    ALLOWED_ATTRIBUTES = {
        "a": {"href", "title"},
        "abbr": {"title"},
        "td": {"colspan", "rowspan"},
        "th": {"colspan", "rowspan", "scope"},
    }
    SAFE_LINK_SCHEMES = {"http", "https", "mailto", "tel"}

    @classmethod
    def sanitize(cls, value: str | None) -> SanitizedEmailBody:
        soup = BeautifulSoup(str(value or ""), "html.parser")

        for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
            comment.extract()

        for tag in list(soup.find_all(True)):
            if tag.parent is None:
                continue
            name = (tag.name or "").casefold()
            if name in cls.DROP_WITH_CONTENT:
                tag.decompose()
                continue
            if name not in cls.ALLOWED_TAGS:
                tag.unwrap()
                continue

            allowed_attributes = cls.ALLOWED_ATTRIBUTES.get(name, set())
            for attribute in list(tag.attrs):
                if attribute.casefold() not in allowed_attributes:
                    del tag.attrs[attribute]

            if name == "a":
                href = cls._safe_link(tag.get("href"))
                if href:
                    tag["href"] = href
                    tag["target"] = "_blank"
                    tag["rel"] = "nofollow noopener noreferrer"
                else:
                    tag.attrs.pop("href", None)

            for numeric_attribute in ("colspan", "rowspan"):
                if numeric_attribute in tag.attrs:
                    value = str(tag.attrs[numeric_attribute]).strip()
                    if not value.isdigit() or not 1 <= int(value) <= 100:
                        del tag.attrs[numeric_attribute]

        html = str(soup).strip()
        text = soup.get_text(separator="\n", strip=True)
        return SanitizedEmailBody(html=html, text=text)

    @classmethod
    def _safe_link(cls, value: object) -> str:
        href = "".join(str(value or "").split()).strip()
        if not href:
            return ""
        if href.startswith("#"):
            return href
        try:
            parsed = urlsplit(href)
        except ValueError:
            return ""
        if parsed.scheme.casefold() not in cls.SAFE_LINK_SCHEMES:
            return ""
        return href
