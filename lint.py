#!/usr/bin/env python3

"""
Linter - 自动检查笔记仓库的规则遵守情况
所有配置从索引文件的 YAML frontmatter 中读取
"""

import re
import sys
import io
import urllib.parse
from dataclasses import dataclass, field, replace, fields
from pathlib import Path
from typing import Optional, Iterator
import yaml

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

ROOT = Path(__file__).resolve().parent
SPECIAL_DIRS = {'attachments', '__pycache__'}

# ================= 预编译正则表达式 =================
FM_PATTERN = re.compile(r'^---\n(.*?)\n---\n', re.DOTALL)
TITLE_PATTERN = re.compile(r'^#\s+(.+)$', re.MULTILINE)
# 捕获两部分：1.链接文本 2.链接目标
MD_LINK_PATTERN = re.compile(r'\[([^\]]*)\]\(([^)]+)\)')
WIKI_LINK_PATTERN = re.compile(r'\[\[([^\]]+)\]\]')
EMOJI_PATTERN = re.compile(
    r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF'
    r'\U0001F1E0-\U0001F1FF\U00002702-\U00002702\U00002600-\U000026FF'
    r'\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF]'
)

# 全局文件缓存，加速 wiki 链接查找
_FILE_CACHE: Optional[dict[str, Path]] = None


@dataclass
class LintConfig:
    max_file_count: int = 20
    abandon_emoji: bool = True
    require_index: bool = True
    check_links: bool = True
    enforce_title_match: bool = True
    check_index_coverage: bool = True


@dataclass
class LintResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)


# ================= 辅助函数 =================

def get_valid_items(dir_path: Path) -> Iterator[Path]:
    """生成目录下的有效项目（排除隐藏文件和特殊目录）"""
    return (p for p in dir_path.iterdir() if not p.name.startswith('.') and p.name not in SPECIAL_DIRS)


def get_file_cache() -> dict[str, Path]:
    """构建全局文件缓存，将时间复杂度从 O(N*M) 降至 O(1)"""
    global _FILE_CACHE
    if _FILE_CACHE is None:
        _FILE_CACHE = {}
        for p in ROOT.rglob('*'):
            if p.is_file() and not p.name.startswith('.') and not set(p.parts) & SPECIAL_DIRS:
                _FILE_CACHE[p.name] = p
                _FILE_CACHE[p.stem] = p
    return _FILE_CACHE


def merge_config(parent: LintConfig, child_constraints: dict) -> LintConfig:
    valid_keys = {f.name for f in fields(LintConfig)}
    updates = {k: v for k, v in child_constraints.items() if k in valid_keys}
    return replace(parent, **updates)


def get_dir_config(dir_path: Path, parent_config: LintConfig) -> LintConfig:
    index_file = dir_path / f"{dir_path.name}.md"
    if not index_file.exists():
        return parent_config

    content = index_file.read_text(encoding='utf-8')
    match = FM_PATTERN.match(content)
    if match:
        fm = yaml.safe_load(match.group(1)) or {}
        if 'constraints' in fm:
            return merge_config(parent_config, fm['constraints'])
    return parent_config


def find_file_global(filename: str) -> Optional[Path]:
    """全局极速查找文件"""
    filename = filename.replace('\\', '/')
    if '/' in filename:
        exact_path = ROOT / filename.lstrip('/')
        if exact_path.exists(): return exact_path
        if exact_path.with_suffix('.md').exists(): return exact_path.with_suffix('.md')
    
    cache = get_file_cache()
    name = Path(filename).name
    return cache.get(name) or cache.get(f"{name}.md")


# ================= 核心检查逻辑 =================

def check_emoji(content: str, config: LintConfig) -> list[str]:
    if not config.abandon_emoji:
        return []
    return [f"Line {i}: 包含 emoji 字符" for i, line in enumerate(content.splitlines(), 1) if EMOJI_PATTERN.search(line)]


def check_title_match(filepath: Path, content: str, config: LintConfig) -> list[str]:
    if not config.enforce_title_match:
        return []
        
    match = TITLE_PATTERN.search(content)
    if not match:
        return ["缺少标题"]

    title = match.group(1)
    expected = filepath.stem.replace('%20', ' ')
    return [f"标题 '{title}' 与文件名 '{filepath.name}' 不匹配"] if title != expected else []


def check_file_count(dir_path: Path, config: LintConfig) -> list[str]:
    count = sum(1 for _ in get_valid_items(dir_path))
    return [f"目录包含 {count} 个项目，超过上限 {config.max_file_count}"] if count > config.max_file_count else []


def check_links(content: str, filepath: Path, config: LintConfig) -> list[str]:
    if not config.check_links:
        return []

    errors = []
    base_dir = filepath.parent

    # 1. 检查常规 Markdown 链接
    for match in MD_LINK_PATTERN.finditer(content):
        _, raw_link = match.groups()
        
        # 处理可能附带的 title，如 (1.md "Title") -> 截断保留 1.md
        link = raw_link.strip()
        if ' "' in link or " '" in link:
            link = re.split(r'\s+["\']', link)[0]
            
        link = urllib.parse.unquote(link.split('#', 1)[0]).rstrip('/')
        
        if not link or link.startswith(('http://', 'https://', 'mailto:', 'tel:')):
            continue

        if link.startswith('[[') and link.endswith(']]'):
            link = link[2:-2]  # 转交 Wiki 链接逻辑处理
            is_wiki = True
        else:
            is_wiki = False
            link_path = ROOT / link.lstrip('/') if link.startswith('/') else (base_dir / link).resolve()
            # 增强检测：如果文件不存在，且补充 .md 后缀后也不存在，才判定为无效
            if not link_path.exists() and not link_path.with_suffix('.md').exists():
                errors.append(f"无效链接: {match.group(0)}")

        if is_wiki and not find_file_global(link):
            errors.append(f"无效链接: {match.group(0)} (文件未找到)")

    # 2. 检查 Wiki 链接
    for match in WIKI_LINK_PATTERN.finditer(content):
        link = urllib.parse.unquote(match.group(1).split('#', 1)[0]).rstrip('/')
        if not link:
            continue
        if not find_file_global(link):
            errors.append(f"无效链接: [[{match.group(1)}]] (文件未找到)")

    return errors


def check_index_coverage(dir_path: Path, config: LintConfig) -> list[str]:
    if not config.check_index_coverage:
        return []

    index_file = dir_path / f"{dir_path.name}.md"
    if not index_file.exists():
        return []

    content = index_file.read_text(encoding='utf-8')
    linked_items = set()
    
    for m in MD_LINK_PATTERN.finditer(content):
        raw_link = m.group(2).strip()
        link = urllib.parse.unquote(raw_link.split('#', 1)[0]).rstrip('/')
        if ' "' in link or " '" in link:
            link = re.split(r'\s+["\']', link)[0]
        linked_items.add(link.lstrip('/'))

    for m in WIKI_LINK_PATTERN.finditer(content):
        link = urllib.parse.unquote(m.group(1).split('#', 1)[0]).rstrip('/').lstrip('/')
        linked_items.add(link)
        
    errors = []
    for item in get_valid_items(dir_path):
        if item == index_file:
            continue
            
        rel_path = str(item.relative_to(dir_path)).replace('\\', '/')
        if (item.name not in linked_items and 
            rel_path not in linked_items and
            item.stem not in linked_items):
            kind = "目录" if item.is_dir() else "文件"
            errors.append(f"索引文件中未链接{kind}: {item.name}")

    return errors


# ================= 流程控制 =================

def lint_file(filepath: Path, config: LintConfig) -> LintResult:
    result = LintResult()
    try:
        content = filepath.read_text(encoding='utf-8')
        result.errors.extend(check_title_match(filepath, content, config))
        result.errors.extend(check_emoji(content, config))
        result.errors.extend(check_links(content, filepath, config))
    except Exception as e:
        result.errors.append(f"无法读取文件: {e}")
    return result


def lint_directory(dir_path: Path, parent_config: LintConfig) -> LintResult:
    config = get_dir_config(dir_path, parent_config)
    result = LintResult()

    result.errors.extend(check_file_count(dir_path, config))
    
    is_special = dir_path.name in SPECIAL_DIRS
    if config.require_index and not is_special:
        expected_index = dir_path / f"{dir_path.name}.md"
        if not expected_index.exists() and dir_path != ROOT:
            result.errors.append(f"缺少索引文件: {expected_index.name}")

    result.errors.extend(check_index_coverage(dir_path, config))

    # 【关键修复】：移除原先跳过处理索引文件 (dir_path.name + ".md") 的 continue 逻辑
    # 以确保写在索引文件内部的无效链接同样受到严格检查
    for md_file in dir_path.glob("*.md"):
        res = lint_file(md_file, config)
        result.errors.extend(f"{md_file.name}: {err}" for err in res.errors)
        result.warnings.extend(f"{md_file.name}: {warn}" for warn in res.warnings)

    # 递归子目录
    for subdir in get_valid_items(dir_path):
        if subdir.is_dir():
            sub_res = lint_directory(subdir, config)
            result.errors.extend(f"[{subdir.name}] {err}" for err in sub_res.errors)
            result.warnings.extend(f"[{subdir.name}] {warn}" for warn in sub_res.warnings)

    return result


def main():
    result = lint_directory(ROOT, LintConfig())

    if result.errors:
        print("[ERROR] Errors found:")
        for err in result.errors: print(f"  - {err}")

    if result.warnings:
        print("[WARNING] Warnings:")
        for warn in result.warnings: print(f"  - {warn}")

    if not result.has_errors:
        if not result.warnings:
            print("[OK] All checks passed")
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()