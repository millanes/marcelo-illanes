from datetime import datetime, timedelta, timezone

from sistema.reloj import RelojPlanta

ARGENTINA = timezone(timedelta(hours=-3))


def test_sin_mensajes_usa_la_hora_de_la_maquina():
    reloj = RelojPlanta()
    assert not reloj.sincronizado
    assert abs(reloj.ahora() - datetime.now(timezone.utc)) < timedelta(seconds=5)


def test_toma_la_hora_del_ultimo_mensaje_aunque_vuelva_para_atras():
    reloj = RelojPlanta()
    despues = datetime(2026, 11, 16, 15, 0, tzinfo=ARGENTINA)
    antes = despues - timedelta(days=3)
    reloj.observar(despues)
    assert reloj.sincronizado and reloj.ahora() == despues
    reloj.observar(antes)   # cambió el escenario
    assert reloj.ahora() == antes


def test_una_hora_sin_zona_se_toma_como_utc():
    reloj = RelojPlanta()
    reloj.observar(datetime(2026, 11, 16, 18, 0))
    assert reloj.ahora() == datetime(2026, 11, 16, 15, 0, tzinfo=ARGENTINA)
