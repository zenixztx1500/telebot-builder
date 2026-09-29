# TeleBot Builder

Painel web (Flask + python-telegram-bot v21) para criar e gerenciar bots do Telegram sem escrever código.

## Instalar e rodar
```bash
pip install -r requirements.txt
python app.py
```
Abra http://127.0.0.1:5000

## Uso
1. Crie um bot no @BotFather e copie o token.
2. Clique em "+ Novo bot" e cole o token.
3. Configure comandos, respostas automáticas, botões, filtros e permissões e clique em Salvar (o bot reinicia sozinho).
4. A aba Log mostra o que o bot recebeu e enviou. Se aparecer ERR, o token ou a internet estão com problema.

## Pagamento automático (Pix por API)
Na aba **Pagamento**, escolha "Atendimento automático (API de pagamento)" e o intermediador:

| Intermediador | Onde pegar o token | Testes |
|---|---|---|
| **PagBank** | acesso.pagbank.com.br → Venda online → Integrações → Gerar Token | Ambiente Sandbox. Produção exige liberação da API pelo suporte do PagBank |
| **Mercado Pago** | mercadopago.com.br/developers → Suas integrações → Credenciais → Access Token | Token `TEST-...` = testes, `APP_USR-...` = vendas reais |
| **Asaas** | Integrações → Chave de API (`$aact_...`) | Ambiente Sandbox (conta em sandbox.asaas.com) |

1. Cole o token e clique em **Testar token**: ele é conferido no intermediador (e o painel avisa se for token de testes no modo produção ou vice-versa). O token nunca volta para o navegador.
2. Informe seu ID do Telegram para receber o aviso de cada venda paga (mande /start para o seu bot uma vez).
3. A conta no intermediador precisa ter uma chave Pix cadastrada.

Bancos tradicionais (Itaú, Bradesco, BB, Santander...) não estão na lista porque a API Pix deles exige contrato de empresa e certificado digital, não só um token.

No bot, o cliente informa nome, CPF e e-mail na primeira compra, recebe o QR Code e o Pix copia e cola com o valor exato, e o pagamento é confirmado sozinho (verificação a cada 30s; os pedidos ficam na tabela `pix_orders`).

Dados ficam em `data/bots.json` (contém os tokens: não compartilhe essa pasta).
O painel escuta só em 127.0.0.1. Não exponha na rede sem proteger com senha.
