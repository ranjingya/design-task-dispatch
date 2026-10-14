"""Word 图文提取及旧版格式转换测试。"""

import io
import json
import shutil
import subprocess
from zipfile import ZipFile

import pytest
from PIL import Image

from app.config import ROOT, Settings
from app.documents import read_document
from app.materials import MaterialBundle


@pytest.fixture
def bundle(tmp_path):
    settings = Settings('app', 'secret', 'base', 'table', 'token', tmp_path / 'fields.json')
    return MaterialBundle(settings, json.loads((ROOT / 'config/materials.json').read_text()))


def word_bytes(external=False):
    """构造含正文、表格、页眉和图片的可打开 Word 文档。"""
    out, image = io.BytesIO(), io.BytesIO()
    Image.new('RGB', (60, 40), 'red').save(image, format='PNG')
    with ZipFile(out, 'w') as archive:
        archive.writestr('[Content_Types].xml', '''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/></Types>''')
        archive.writestr('_rels/.rels', '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="doc" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>''')
        archive.writestr('word/document.xml', '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><w:body><w:p><w:r><w:t>Create product images 修改要求见图</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>尺寸750</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>数量5</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:drawing><wp:inline><wp:extent cx="571500" cy="381000"/><wp:docPr id="1" name="图"/><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic><pic:nvPicPr><pic:cNvPr id="1" name="图"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:embed="pic"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="571500" cy="381000"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p><w:sectPr><w:headerReference w:type="default" r:id="header"/></w:sectPr></w:body></w:document>''')
        target = 'https://outside.example/image.png' if external else 'media/image.png'
        mode = ' TargetMode="External"' if external else ''
        archive.writestr('word/_rels/document.xml.rels', f'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="pic" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="{target}"{mode}/><Relationship Id="header" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/></Relationships>''')
        archive.writestr('word/header1.xml', '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>页眉要求</w:t></w:r></w:p></w:hdr>')
        archive.writestr('word/media/image.png', image.getvalue())
    return out.getvalue()


def test_docx_text_table_header_and_image(bundle):
    read_document(word_bytes(), '.docx', '需求.docx', bundle)
    assert [row['text'] for row in bundle.texts] == ['Create product images 修改要求见图', '尺寸750', '数量5', '页眉要求']
    assert len(bundle.images) == 1 and not bundle.issues
    assert bundle.image_sources[0]['requires_detail']


def test_docx_external_image_reports_missing_material(bundle):
    read_document(word_bytes(True), '.docx', '需求.docx', bundle)
    assert len(bundle.images) == 0
    assert any('外部链接' in issue for issue in bundle.issues)


def test_docx_unpacked_limit(bundle):
    bundle.config['max_unpacked_bytes'] = 1
    with pytest.raises(ValueError, match='解压'):
        read_document(word_bytes(), '.docx', '需求', bundle)


def test_legacy_doc_signature_and_converter_missing(bundle, monkeypatch):
    with pytest.raises(ValueError, match='格式无效'):
        read_document(b'not word', '.doc', '需求', bundle)
    monkeypatch.setattr('subprocess.run', lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(RuntimeError, match='未安装'):
        read_document(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1', '.doc', '需求', bundle)


@pytest.mark.skipif(not shutil.which('soffice'), reason='本机未安装 LibreOffice，容器内提供转换器')
def test_real_legacy_word_conversion(bundle, tmp_path):
    """实际生成旧版 DOC，再验证转换后的正文及页面图片。"""
    source = tmp_path / 'example.docx'
    source.write_bytes(word_bytes())
    subprocess.run(['soffice', '-env:UserInstallation=' + (tmp_path / 'create-profile').as_uri(), '--headless',
                    '--convert-to', 'doc:MS Word 97', '--outdir', str(tmp_path), str(source)], check=True,
                   timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    data = (tmp_path / 'example.doc').read_bytes()
    read_document(data, '.doc', '旧Word', bundle)
    assert bundle.images
    assert any('Create product images' in row['text'] for row in bundle.texts)
    assert not bundle.issues
