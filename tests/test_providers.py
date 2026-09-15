"""Tests de filtrado de calidad de los proveedores (H5P y EduHoot)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.providers.eduhoot import EduHootProvider  # noqa: E402
from app.providers.h5p import H5POERHubProvider, _age_is_eligible, _parse_age  # noqa: E402


def test_parse_age():
    assert _parse_age("12-16") == (12, 16)
    assert _parse_age("11-") == (11, None)
    assert _parse_age("14") == (14, 14)
    assert _parse_age("") == (None, None)
    assert _parse_age("preschool") == (None, None)


def test_age_is_eligible():
    # Sin edad → se acepta (relajado).
    assert _age_is_eligible("") is True
    # Escolar y juvenil.
    assert _age_is_eligible("4-7") is True
    assert _age_is_eligible("12-16") is True
    assert _age_is_eligible("12-18") is True
    assert _age_is_eligible("5-20") is True
    assert _age_is_eligible("17") is True
    # Adulto explícito (mín >= 18) → fuera del banco.
    assert _age_is_eligible("18") is False
    assert _age_is_eligible("18-99") is False
    assert _age_is_eligible("20-40") is False
    assert _age_is_eligible("25-65") is False


def test_h5p_eligibility():
    p = H5POERHubProvider()
    assert p._is_eligible({"language": "es", "age": "12-16"}) is True
    assert p._is_eligible({"language": "de", "age": "8-11"}) is True
    assert p._is_eligible({"language": "de", "age": ""}) is True  # sin edad
    assert p._is_eligible({"language": "pt-br", "age": "10"}) is True
    assert p._is_eligible({"language": "eu", "age": "10"}) is True  # euskera sí
    assert p._is_eligible({"language": "gl", "age": "10"}) is True  # gallego sí
    # Adulto explícito o idioma fuera del alcance de H5P (ruso/latín/chino).
    assert p._is_eligible({"language": "es", "age": "18-99"}) is False
    assert p._is_eligible({"language": "ru", "age": "12-16"}) is False
    assert p._is_eligible({"language": "ru", "age": ""}) is False
    assert p._is_eligible({"language": "la", "age": ""}) is False
    assert p._is_eligible({"language": "zh", "age": "10"}) is False


def test_eduhoot_non_educational_blocked():
    p = EduHootProvider()
    assert p._is_educational({"name": "Minecraft", "tags": ["minecraft", "generado-ia"]}) is False
    assert p._is_educational({"name": "Aitana", "tags": ["espanol", "actualidad"]}) is False
    assert p._is_educational({"name": "Spiderman", "tags": ["peliculas-y-actores"]}) is False
    assert p._is_educational({"name": "Hang the DJ (Black Mirror)", "tags": []}) is False
    assert p._is_educational({"name": "Ready Player One", "tags": []}) is False
    assert p._is_educational({"name": "Roblox", "tags": ["primaria", "valencia"]}) is False
    assert p._is_educational({"name": "Clash Royale", "tags": ["espanol"]}) is False
    assert p._is_educational({"name": "Marvel", "tags": ["espanol"]}) is False


def test_eduhoot_educational_kept():
    p = EduHootProvider()
    assert p._is_educational({"name": "Nombre de las notas musicales", "tags": ["musica", "primaria"]}) is True
    assert p._is_educational({"name": "Números enteros", "tags": ["2-eso", "matematicas"]}) is True
    assert p._is_educational({"name": "Divisions", "tags": []}) is True


SAMPLE_JCLIC = """<?xml version="1.0" encoding="UTF-8"?>
<JClicProject name="criptacoloniaguell" version="0.1.3">
 <settings>
  <title>La Cripta de la Colònia Güell</title>
  <description>
   <p>Aquest paquet d'activitats té l'objectiu de conèixer la Cripta.</p>
  </description>
  <descriptions>
   <description language="ca"><p>Descripció catalana.</p></description>
   <description language="en"><p>English description.</p></description>
   <description language="es"><p>Descripción española.</p></description>
  </descriptions>
  <license type="by-nc-sa" url="https://creativecommons.org/licenses/by-nc-sa/4.0" />
 </settings>
</JClicProject>
"""


def test_extract_jclic_description():
    from app.providers.jclic import _extract_description

    d = _extract_description(SAMPLE_JCLIC)
    assert d["description"] == "Descripción española."
    assert d["description_ca"] == "Descripció catalana."
    assert d["description_en"] == "English description."
    assert d["license"] == "by-nc-sa"


def test_extract_jclic_description_no_multilingual():
    from app.providers.jclic import _extract_description

    xml = """<JClicProject><settings>
    <description><p>Descripció única.</p></description>
    <license type="by" />
    </settings></JClicProject>"""
    d = _extract_description(xml)
    assert d["description"] == "Descripció única."
    assert d["description_ca"] == ""
    assert d["description_en"] == ""
    assert d["license"] == "by"
