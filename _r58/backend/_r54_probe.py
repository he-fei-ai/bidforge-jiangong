import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.services.facts_classification import (
    classify_chapter_from_text, classify_fact_attr,
)

print("A", classify_fact_attr("基坑开挖深度", "基坑开挖深度"))
print("B", classify_fact_attr("基坑开挖深度", "12.5m"))
print("C", classify_chapter_from_text("工程地点", "北京市朝阳区", "basic", "", ""))
print("D", classify_fact_attr("工程地点", "北京市朝阳区"))
print("E", classify_chapter_from_text("基坑开挖深度", "基坑开挖深度", "tech_param", "", ""))
