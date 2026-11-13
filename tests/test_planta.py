import httpx
import pytest

from sistema.planta import ClientePlanta


def cliente(manejador) -> ClientePlanta:
    return ClientePlanta("http://sim:8100", espera_s=0, transporte=httpx.MockTransport(manejador))


def test_reintenta_un_error_del_servidor():
    pedidos = []

    def manejador(pedido):
        pedidos.append(pedido)
        return httpx.Response(503) if len(pedidos) == 1 else httpx.Response(200, content=b"RIFF")

    assert cliente(manejador).descargar("/media/audios/NOTA-1.wav") == b"RIFF"
    assert len(pedidos) == 2


def test_un_404_no_se_reintenta():
    pedidos = []

    def manejador(pedido):
        pedidos.append(pedido)
        return httpx.Response(404)

    with pytest.raises(httpx.HTTPStatusError):
        cliente(manejador).descargar("/media/audios/no-existe.wav")
    assert len(pedidos) == 1


def test_sin_red_se_rinde_despues_de_los_reintentos():
    pedidos = []

    def manejador(pedido):
        pedidos.append(pedido)
        raise httpx.ConnectError("sin red", request=pedido)

    with pytest.raises(httpx.ConnectError):
        cliente(manejador).planta()
    assert len(pedidos) == 3
