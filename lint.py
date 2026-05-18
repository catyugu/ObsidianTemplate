#!/usr/bin/env python3

"""
Linter - 自动检查笔记仓库的规则遵守情况
所有配置从索引文件的 YAML frontmatter 中读取
"""

import re
import sys
import io
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import yaml

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 自动检测 ROOT 目录（脚本所在目录）
ROOT = Path(__file__).resolve().parent

@dataclass
class LintConfig:
    """规则配置"""
    max_file_count: int = 20
    abandon_emoji: bool = True  # 默认禁用 emoji
    require_index: bool = True
    check_links: bool = True
    enforce_title_match: bool = True
    check_index_coverage: bool = True  # 检查索引覆盖率


@dataclass
class LintResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, msg: str):
        self.errors.append(msg)

    def add_warning(self, msg: str):
        self.warnings.append(msg)

    def has_errors(self) -> bool:
        return len(self.errors) > 0


# 特殊目录（不需要索引文件）
SPECIAL_DIRS = {'attachments', '__pycache__'}


def read_yaml_frontmatter(content: str) -> Optional[dict]:
    """读取 markdown 文件的 YAML frontmatter"""
    pattern = r'^---\n(.*?)\n---\n'
    match = re.match(pattern, content, re.DOTALL)
    if match:
        return yaml.safe_load(match.group(1))
    return None


def extract_title(content: str) -> Optional[str]:
    """提取 markdown 文件的第一个标题"""
    pattern = r'^#\s+(.+)$'
    match = re.search(pattern, content, re.MULTILINE)
    return match.group(1) if match else None


def merge_config(parent: LintConfig, child_constraints: dict) -> LintConfig:
    """合并父子配置，子配置覆盖父配置"""
    config = LintConfig(
        max_file_count=child_constraints.get('max_file_count', parent.max_file_count),
        abandon_emoji=child_constraints.get('abandon_emoji', parent.abandon_emoji),
        require_index=child_constraints.get('require_index', parent.require_index),
        check_links=child_constraints.get('check_links', parent.check_links),
        enforce_title_match=child_constraints.get('enforce_title_match', parent.enforce_title_match),
        check_index_coverage=child_constraints.get('check_index_coverage', parent.check_index_coverage)
    )
    return config


def get_dir_config(dir_path: Path, parent_config: LintConfig) -> tuple[LintConfig, dict]:
    """获取目录的配置，返回 (合并后的配置, 该目录的原始约束字典)"""
    index_file = dir_path / f"{dir_path.name}.md"
    if not index_file.exists():
        return parent_config, {}

    content = index_file.read_text(encoding='utf-8')
    fm = read_yaml_frontmatter(content)
    if not fm or 'constraints' not in fm:
        return parent_config, {}

    return merge_config(parent_config, fm['constraints']), fm['constraints']


def check_emoji(content: str, config: LintConfig) -> list[str]:
    """检查是否包含 emoji"""
    if not config.abandon_emoji:
        return []

    emoji_pattern = re.compile(
        r'[\U0001F600-\U0001F64F'
        r'\U0001F300-\U0001F5FF'
        r'\U0001F680-\U0001F6FF'
        r'\U0001F1E0-\U0001F1FF'
        r'\U00002702-\U00002702'
        r'\U00002600-\U000026FF'
        r'\U0001F900-\U0001F9FF'
        r'\U0001FA00-\U0001FA6F'
        r'\U0001FA70-\U0001FAFF]'
    )
    errors = []
    for i, line in enumerate(content.split('\n'), 1):
        if emoji_pattern.search(line):
            errors.append(f"Line {i}: 包含 emoji 字符")
    return errors


def check_title_match(filepath: Path, content: str, config: LintConfig) -> list[str]:
    """检查标题是否与文件名匹配"""
    if not config.enforce_title_match:
        return []

    title = extract_title(content)
    if not title:
        return [f"缺少标题"]

    expected = filepath.stem.replace('%20', ' ')
    if title != expected:
        return [f"标题 '{title}' 与文件名 '{filepath.name}' 不匹配"]
    return []


def check_file_count(dir_path: Path, config: LintConfig) -> list[str]:
    """检查目录下的文件数量"""
    items = [p for p in dir_path.iterdir()
             if not p.name.startswith('.') and p.name not in SPECIAL_DIRS]
    count = len(items)

    if count > config.max_file_count:
        return [f"目录包含 {count} 个项目，超过上限 {config.max_file_count}"]
    return []


def find_file_global(filename: str) -> Optional[Path]:
    """Globally search for a file by name across the entire repository"""
    # Remove extension if present to search for both with and without extension
    name_without_ext = Path(filename).stem
    ext = Path(filename).suffix

    # Normalize path separators for cross-platform compatibility
    filename = filename.replace('\\', '/')
    has_path = '/' in filename

    # Build search patterns - prioritize exact path matches first
    patterns = []
    if has_path:
        # Wiki link with path - try exact path match first, then filename-only fallback
        patterns.append(filename)
        patterns.append(name_without_ext)
        if ext:
            patterns.append(name_without_ext)
        else:
            patterns.append(f"{name_without_ext}.md")
    else:
        # Wiki link without path - try filename with and without extension
        patterns.append(name_without_ext)
        if ext:
            patterns.append(filename)
        else:
            patterns.append(f"{name_without_ext}.md")

    for pattern in patterns:
        # Search recursively from ROOT
        for path in ROOT.rglob(pattern):
            if path.is_file():
                return path
    return None


def check_links(content: str, filepath: Path, config: LintConfig) -> list[str]:
    """检查链接有效性"""
    if not config.check_links:
        return []

    errors = []
    # Markdown style links: [text](link)
    md_link_pattern = re.compile(r'\[.*?\]\(([^)]+)\)')
    # Wiki style links: [[link]]
    wiki_link_pattern = re.compile(r'\[\[([^\]]+)\]\]')

    base_dir = filepath.parent

    # Check markdown style links
    for match in md_link_pattern.finditer(content):
        link = match.group(1)

        # 跳过外部URL
        if link.startswith('http://') or link.startswith('https://'):
            continue

        # 分离链接和锚点
        anchor = ''
        if '#' in link:
            link, anchor = link.split('#', 1)
            # 纯锚点链接（如 #heading）跳过
            if not link:
                continue

        # 解码 URL 编码（%20 -> 空格等）
        decoded_link = urllib.parse.unquote(link)

        # 处理 WIKI 式链接 [[link]]
        is_wiki_link = False
        if decoded_link.startswith('[[') and decoded_link.endswith(']]'):
            decoded_link = decoded_link[2:-2]
            is_wiki_link = True

        # 去除末尾的 / 使其与目录名匹配
        decoded_link = decoded_link.rstrip('/')

        if is_wiki_link:
            # Wiki-style link: globally search for the file
            found_path = find_file_global(decoded_link)
            if not found_path:
                errors.append(f"链接无效: {link} (文件未找到)")
        else:
            # 判断是绝对路径还是相对路径
            if decoded_link.startswith('/'):
                # 绝对路径（根目录）
                link_path = ROOT / decoded_link.lstrip('/')
            else:
                # 相对路径
                link_path = (base_dir / decoded_link).resolve()

            # 检查文件或目录是否存在
            if not link_path.exists():
                errors.append(f"链接无效: {link}")

    # Check wiki style links separately (global search)
    for match in wiki_link_pattern.finditer(content):
        link = match.group(1)

        # 解码 URL 编码（%20 -> 空格等）
        decoded_link = urllib.parse.unquote(link)
        decoded_link = decoded_link.rstrip('/')

        found_path = find_file_global(decoded_link)
        if not found_path:
            errors.append(f"链接无效: [[{decoded_link}]] (文件未找到)")

    return errors


def check_index_coverage(dir_path: Path, config: LintConfig) -> list[str]:
    """检查索引文件的覆盖率 - 是否所有文件都被索引"""
    if not config.check_index_coverage:
        return []

    index_file = dir_path / f"{dir_path.name}.md"
    if not index_file.exists():
        return []

    content = index_file.read_text(encoding='utf-8')

    # 获取目录下所有需要索引的文件和目录（排除特殊目录和隐藏文件）
    all_items = []
    for item in dir_path.iterdir():
        if not item.name.startswith('.') and item.name not in SPECIAL_DIRS:
            all_items.append(item)

    # 提取索引文件中链接的所有文件和目录（不仅仅是 .md 文件）
    linked_items = set()
    rel_path_pattern = re.compile(r'\[.*?\]\(([^)]+)\)')
    for match in rel_path_pattern.finditer(content):
        link = match.group(1)
        decoded_link = urllib.parse.unquote(link)
        # 去除末尾的 /
        decoded_link = decoded_link.rstrip('/')
        if decoded_link.startswith('/'):
            linked_items.add(decoded_link.lstrip('/'))
        else:
            linked_items.add(decoded_link)

    errors = []
    for item in all_items:
        # 跳过索引文件本身
        if item.name == index_file.name:
            continue
        # 检查目录/文件是否在链接中（允许文件名或路径形式）
        item_linked = False
        for linked in linked_items:
            if item.name == linked or item.name == linked.replace('%20', ' '):
                item_linked = True
                break
            # 也检查带路径的链接
            if str(item.relative_to(dir_path)) == linked or str(item.relative_to(dir_path)).replace('%20', ' ') == linked:
                item_linked = True
                break

        if not item_linked:
            if item.is_dir():
                errors.append(f"索引文件中未链接目录: {item.name}")
            else:
                errors.append(f"索引文件中未链接: {item.name}")

    return errors

def lint_file(filepath: Path, config: LintConfig) -> LintResult:
    """检查单个文件"""
    result = LintResult()

    try:
        content = filepath.read_text(encoding='utf-8')
    except Exception as e:
        result.add_error(f"无法读取文件: {e}")
        return result

    result.errors.extend(check_title_match(filepath, content, config))
    result.errors.extend(check_emoji(content, config))
    result.errors.extend(check_links(content, filepath, config))

    return result


def lint_directory(dir_path: Path, parent_config: LintConfig) -> LintResult:
    """检查整个目录"""
    config, _ = get_dir_config(dir_path, parent_config)
    result = LintResult()

    # 检查文件数量
    result.errors.extend(check_file_count(dir_path, config))

    # 检查索引文件（特殊目录除外）
    if config.require_index and dir_path.name not in SPECIAL_DIRS:
        expected_index = dir_path / f"{dir_path.name}.md"
        if not expected_index.exists():
            result.add_error(f"缺少索引文件: {expected_index.name}")

    # 检查索引覆盖率
    result.errors.extend(check_index_coverage(dir_path, config))

    # 检查目录下所有 markdown 文件
    for md_file in dir_path.glob("*.md"):
        if md_file.name == dir_path.name + ".md" and dir_path != ROOT:
            continue
        file_result = lint_file(md_file, config)
        for err in file_result.errors:
            result.add_error(f"{md_file.name}: {err}")
        for warn in file_result.warnings:
            result.add_warning(f"{md_file.name}: {warn}")

    # 递归检查子目录（排除特殊目录）
    for subdir in dir_path.iterdir():
        if subdir.is_dir() and not subdir.name.startswith('.') and subdir.name not in SPECIAL_DIRS:
            sub_result = lint_directory(subdir, config)
            for err in sub_result.errors:
                result.add_error(f"[{subdir.name}] {err}")
            for warn in sub_result.warnings:
                result.add_warning(f"[{subdir.name}] {warn}")

    return result


def run_lint() -> LintResult:
    """运行所有检查"""
    root_config = LintConfig()
    return lint_directory(ROOT, root_config)


def main():
    result = run_lint()

    if result.errors:
        print("[ERROR] Errors found:")
        for err in result.errors:
            print(f"  - {err}")

    if result.warnings:
        print("[WARNING] Warnings:")
        for warn in result.warnings:
            print(f"  - {warn}")

    if not result.errors and not result.warnings:
        print("[OK] All checks passed")

    sys.exit(1 if result.has_errors() else 0)


if __name__ == "__main__":
    main()