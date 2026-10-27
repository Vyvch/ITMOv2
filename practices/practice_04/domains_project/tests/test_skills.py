import unittest
import os

class TestSkills(unittest.TestCase):
    def setUp(self):
        # Путь к папке со скиллами
        self.skills_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '.agents', 'skills'))
        
    def test_skills_directory_exists(self):
        """Проверяем, что папка со скиллами вообще существует."""
        self.assertTrue(os.path.exists(self.skills_dir), "Папка .agents/skills не найдена")
        self.assertTrue(os.path.isdir(self.skills_dir), ".agents/skills должна быть директорией")

    def test_all_skills_have_valid_markdown(self):
        """Проверяем, что каждый скилл содержит валидный SKILL.md с нужными метаданными."""
        if not os.path.exists(self.skills_dir):
            self.skipTest("Папка скиллов отсутствует")
            
        skill_folders = [f for f in os.listdir(self.skills_dir) if os.path.isdir(os.path.join(self.skills_dir, f))]
        self.assertGreater(len(skill_folders), 0, "Не найдено ни одного скилла в проекте")
        
        for folder in skill_folders:
            skill_md_path = os.path.join(self.skills_dir, folder, 'SKILL.md')
            self.assertTrue(os.path.exists(skill_md_path), f"Скилл {folder} не содержит файл SKILL.md")
            
            with open(skill_md_path, 'r', encoding='utf-8') as f:
                content = f.read()
                
            self.assertGreater(len(content.strip()), 50, f"Файл SKILL.md в {folder} слишком короткий или пустой")
            
            # Проверяем наличие Frontmatter
            self.assertTrue(content.startswith('---'), f"SKILL.md в {folder} должен начинаться с '---' (Frontmatter)")
            
            # Проверяем, что есть name и description
            frontmatter_parts = content.split('---')
            self.assertGreaterEqual(len(frontmatter_parts), 3, f"Некорректный Frontmatter в {folder}")
            
            meta_block = frontmatter_parts[1]
            self.assertIn("name:", meta_block, f"В {folder} отсутствует поле 'name:' в Frontmatter")
            self.assertIn("description:", meta_block, f"В {folder} отсутствует поле 'description:' в Frontmatter")

if __name__ == "__main__":
    unittest.main()
