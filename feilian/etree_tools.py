import re
import html5lib
from lxml import etree
from tokenizers import Tokenizer
from copy import deepcopy
from urllib.parse import unquote
from typing import List, Optional
from collections import defaultdict

from feilian.html_constants import INTERACTIVE_ELEMENTS


# A regex matching the "invalid XML character range"
ILLEGAL_XML_CHARS_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1F\uD800-\uDFFF\uFFFE\uFFFF]"
)


def strip_illegal_xml_characters(s, default, base=10):
    # Compare the "invalid XML character range" numerically
    n = int(s, base)
    if (
        n in (0xB, 0xC, 0xFFFE, 0xFFFF)
        or 0x0 <= n <= 0x8
        or 0xE <= n <= 0x1F
        or 0xD800 <= n <= 0xDFFF
    ):
        return ""
    return default


def remove_control_characters(html: str):
    """
    Strip invalid XML characters that `lxml` cannot parse.
    """

    # See: https://github.com/html5lib/html5lib-python/issues/96
    #
    # The XML 1.0 spec defines the valid character range as:
    # Char ::= #x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] | [#x10000-#x10FFFF]
    #
    # We can instead match the invalid characters by inverting that range into:
    # InvalidChar ::= #xb | #xc | #xFFFE | #xFFFF | [#x0-#x8] | [#xe-#x1F] | [#xD800-#xDFFF]
    #
    # Sources:
    # https://www.w3.org/TR/REC-xml/#charsets,
    # https://lsimons.wordpress.com/2011/03/17/stripping-illegal-characters-out-of-xml-in-python/

    # We encode all non-ascii characters to XML char-refs, so for example "💖" becomes: "&#x1F496;"
    # Otherwise we'd remove emojis by mistake on narrow-unicode builds of Python
    html = html.encode("ascii", "xmlcharrefreplace").decode("utf-8")
    html = re.sub(
        r"&#(\d+);?",
        lambda c: strip_illegal_xml_characters(c.group(1), c.group(0)),
        html,
    )
    html = re.sub(
        r"&#[xX]([0-9a-fA-F]+);?",
        lambda c: strip_illegal_xml_characters(c.group(1), c.group(0), base=16),
        html,
    )
    html = ILLEGAL_XML_CHARS_RE.sub("", html)
    return html


def parse_html(html: str):
    html = remove_control_characters(html)
    return html5lib.parse(html, treebuilder="lxml", namespaceHTMLElements=False)


def post_order_traversal(tree: etree._Element, func):
    for ele in tree.iterchildren():
        post_order_traversal(ele, func)

    func(tree)


def _traverse(root: etree._Element, xpath: str):
    # children
    tag_counts = defaultdict(int)
    for ele in root.iterchildren():
        tag_counts[ele.tag] += 1

    tag_order = defaultdict(int)
    ele: etree._Element
    for ele in root.iterchildren():
        new_xpath = f"{xpath}/{ele.tag}"
        if tag_counts[ele.tag] > 1:
            new_xpath = f"{xpath}/{ele.tag}[{tag_order[ele.tag] + 1}]"
        tag_order[ele.tag] += 1

        yield from _traverse(ele, new_xpath)
        yield (ele, new_xpath)

    yield (root, xpath)


def traverse(tree: etree._Element | etree._ElementTree):
    if isinstance(tree, etree._ElementTree):
        root = tree.getroot()
        if root is None:
            raise ValueError("root is None")
        if root.tag != "html":
            raise ValueError("root tag is not html")

        return _traverse(root, "/html")

    return _traverse(tree, f"/{tree.tag}")


def _pre_order_traversal(tree: Optional[etree._Element], xpath, func):
    if tree is None:
        return

    # pre-order
    func(tree, xpath)

    # children
    tag_counts = defaultdict(int)
    for ele in tree.iterchildren():
        tag_counts[ele.tag] += 1

    tag_order = defaultdict(int)
    for ele in tree.iterchildren():
        if tag_counts[ele.tag] > 1:
            _pre_order_traversal(
                ele, f"{xpath}/{ele.tag}[{tag_order[ele.tag] + 1}]", func
            )
        else:
            _pre_order_traversal(ele, f"{xpath}/{ele.tag}", func)
        tag_order[ele.tag] += 1


def pre_order_traversal(tree: etree._Element | etree._ElementTree, func):
    if isinstance(tree, etree._ElementTree):
        root: etree._Element = tree.getroot()
        _pre_order_traversal(root, f"/{root.tag}", func)
    else:
        _pre_order_traversal(tree, f"/{tree.tag}", func)


def breadth_first_travel(element: etree._Element, callback, enable_interruption=False):
    queue = [element]
    while queue:
        current = queue.pop(0)
        if current is None:
            continue

        should_interrupt = callback(current)
        if enable_interruption and should_interrupt:
            continue

        queue.extend(current.getchildren())


def _remove(element: etree._Element):
    p = element.getparent()
    if p is not None:
        p.remove(element)


def _clean_html(ele: etree._Element):
    # 移除非元素的节点
    if not isinstance(ele, etree._Element):
        _remove(ele)
        return

    # 移除注释 ele.tag.__name__ == "Comment"
    if not isinstance(ele.tag, str) and ele.tag.__name__ == "Comment":
        _remove(ele)
        return

    # 移除交互元素
    if ele.tag in INTERACTIVE_ELEMENTS:
        _remove(ele)
        return

    # 移除空白元素
    # if hasattr(ele, "tag") and not ele.getchildren():
    #     text = ele.text.strip() if ele.text else ""
    #     if not text:
    #         _remove(ele)
    #         return

    # 移除 display:none
    if "style" in ele.attrib and re.search(r"display\s*:\s*none", ele.attrib["style"]):
        ele.clear()
        ele.text = ""
        return

    # 移除多余属性
    if ele.attrib:
        for key in list(ele.attrib.keys()):
            if key not in ["class", "id"]:
                del ele.attrib[key]

        # 移除 href="javascript:*"
        if "href" in ele.attrib and ele.attrib["href"].startswith("javascript:"):
            del ele.attrib["href"]

        # 移除 img src
        if ele.tag == "img" and "src" in ele.attrib:
            del ele.attrib["src"]


def clean_html(ele: etree._Element | etree._ElementTree):
    if isinstance(ele, etree._ElementTree):
        post_order_traversal(ele.getroot(), _clean_html)
    else:
        post_order_traversal(ele, _clean_html)
    return ele


def remove_children(ele: etree._Element):
    for child in ele.getchildren():
        ele.remove(child)
    return ele


def to_string(ele: etree._Element, pretty_print=False):
    html = etree.tostring(ele, encoding="utf-8").decode("utf-8")
    if pretty_print:
        from bs4 import BeautifulSoup

        return BeautifulSoup(html, "html.parser").prettify()
    return html


def prune_by_tokens(
    tokenizer: Tokenizer,
    ele: etree._Element,
    max_tokens: int,
    reversed: bool = False,
):
    if ele is None:
        return

    # 如果总长度小于 max_tokens，不需要修剪
    total_token = len(tokenizer.encode(to_string(ele)).ids)
    if total_token <= max_tokens:
        return

    # check children
    children = ele.getchildren()
    remove_children(ele)
    self_tokens = len(tokenizer.encode(to_string(ele)).ids)
    required_tokens = max_tokens - self_tokens
    if reversed:
        children = reversed(children)

    # no children
    if len(children) == 0:
        return

    acc_tokens = 0
    for idx, child in enumerate(children):
        child_tokens = len(tokenizer.encode(to_string(child)).ids)
        if acc_tokens + child_tokens > required_tokens:
            break
        acc_tokens += child_tokens

    # 保留需要的子节点
    if reversed:
        ele.extend(reversed(children[: idx + 1]))
    else:
        ele.extend(children[: idx + 1])

    # 递归修剪
    prune_by_tokens(tokenizer, child, required_tokens - acc_tokens, reversed=reversed)

    return ele


def parent_xpath(xpath: str):
    return "/".join(xpath.split("/")[:-1])


def get_text_content(ele: etree._Element):
    arr = []
    for t in ele.itertext():
        t = t.strip()
        if t:
            arr.append(t)
    return " ".join(arr)


def replace_with_text(ele: etree._Element):
    text = get_text_content(ele)
    remove_children(ele)
    ele.text = text


def prune_to_text(ele: etree._Element):
    """
    修剪为文本节点
    """
    if len(ele) == 0 and ele.text:
        ele.text = ele.text.strip()
        return

    # 仅保留 td
    if ele.tag == "tr":
        for child in ele.getchildren():
            if child.tag == "td":
                replace_with_text(child)
    # TODO: prune table
    elif ele.tag == "table":
        return ele
    elif ele.tag in {"ul", "ol"}:
        for child in ele.getchildren():
            if child.tag == "li":
                replace_with_text(child)
    else:
        replace_with_text(ele)
    return ele


def deduplicate_to_prune(xpaths: List[str]):
    """
    去重 xpath，保留最上层的节点
    """
    xpaths = sorted(xpaths)
    remove_indexes = set()
    for i in range(len(xpaths)):
        xpath = xpaths[i]

        for j in range(i + 1, len(xpaths)):
            if xpaths[j].startswith(xpath):
                remove_indexes.add(j)

    return [xpaths[i] for i in range(len(xpaths)) if i not in remove_indexes]


def prune_by_xpath(
    ele: etree._Element,
    xpath: str,
    includes: List[str] = [],
):
    """
    根据 xpath 进行修剪
    保留 includes 周围的节点
    return bool: 是否应继续遍历
    """
    is_in_path = any([x.startswith(xpath) for x in includes])
    is_contained = any([xpath.startswith(x) for x in includes])
    if not is_in_path and not is_contained:
        include_parent = any([x.startswith(parent_xpath(xpath)) for x in includes])
        if include_parent:
            ele.clear()
            ele.text = ""
            return False

    return True


def extract_left_subtree(
    tokenizer: Tokenizer,
    element: etree._Element,
    max_tokens: int = 2048,
):
    element = deepcopy(element)
    prune_by_tokens(tokenizer, element, max_tokens, reversed=False)
    return element


def _decode_url(element: etree._Element):
    if element.attrib:
        if "href" in element.attrib:
            element.attrib["href"] = unquote(element.attrib["href"])

        if "src" in element.attrib:
            element.attrib["src"] = unquote(element.attrib["src"])


def decode_url(element: etree._Element):
    post_order_traversal(element, _decode_url)
    return element


def apply_trim_rules(root: etree._Element, rules: List[str]):
    for rule in rules:
        for ele in root.xpath(
            rule, namespaces={"re": "http://exslt.org/regular-expressions"}
        ):
            ele.getparent().remove(ele)
    return root


def extraction_based_pruning(
    tree: etree._Element | etree._ElementTree, includes: List[dict]
):
    pre_order_traversal(
        tree,
        lambda ele, xpath: prune_by_xpath(ele, xpath, includes=includes),
    )


def remove_by_xpath(tree: etree._Element, xpath: str):
    for ele in tree.xpath(
        xpath, namespaces={"re": "http://exslt.org/regular-expressions"}
    ):
        ele.getparent().remove(ele)
