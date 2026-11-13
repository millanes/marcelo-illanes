import httpx

from sistema.enlace import SondaEnlace


def sonda_con(manejador) -> SondaEnlace:
    return SondaEnlace("https://nube.example/v1/models", transporte=httpx.MockTransport(manejador))


def test_cualquier_respuesta_http_quiere_decir_que_hay_enlace():
    assert sonda_con(lambda pedido: httpx.Response(200)).probar() is True
    assert sonda_con(lambda pedido: httpx.Response(405)).probar() is True


def test_si_el_proxy_corta_la_conexion_no_hay_enlace():
    def cortar(pedido):
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=pedido)

    assert sonda_con(cortar).probar() is False


def test_si_no_contesta_a_tiempo_no_hay_enlace():
    def colgarse(pedido):
        raise httpx.ConnectTimeout("timed out", request=pedido)

    assert sonda_con(colgarse).probar() is False


def test_recuerda_el_ultimo_estado():
    respuestas = iter([httpx.ConnectError("caído"), httpx.Response(200)])

    def alternar(pedido):
        r = next(respuestas)
        if isinstance(r, Exception):
            raise r
        return r

    sonda = sonda_con(alternar)
    assert sonda.probar() is False and sonda.ultimo is False
    assert sonda.probar() is True and sonda.ultimo is True
