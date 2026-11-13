import numpy as np
import pytest
from conftest import wav

from sistema.transcripcion import muestras_wav, segundos


def test_wav_de_la_planta_queda_igual():
    x = muestras_wav(wav(2.0))
    assert x.dtype == np.float32 and len(x) == 32000


def test_estereo_se_pasa_a_mono():
    assert len(muestras_wav(wav(1.0, canales=2))) == 16000


def test_otra_frecuencia_se_lleva_a_16_khz():
    assert len(muestras_wav(wav(1.0, tasa=8000))) == 16000


def test_rechaza_lo_que_no_es_pcm_de_16_bits():
    with pytest.raises(ValueError):
        muestras_wav(wav(1.0, bytes_por_muestra=1))


def test_duracion():
    assert segundos(wav(2.5)) == pytest.approx(2.5)
