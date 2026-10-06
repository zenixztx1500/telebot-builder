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

## Pix com valor exato (sem pedir dados do cliente)
Na aba **Pagamento**, escolha "Pix com valor exato (sem dados do cliente)" e informe sua chave Pix, nome, cidade e seu ID do Telegram.
O bot monta o QR Code Pix (BR Code do Banco Central) com o valor exato do carrinho, sem intermediador e sem pedir CPF ou e-mail.
Você recebe cada pedido no Telegram com o botão **✅ Confirmar pagamento**; o cliente tem o botão **Já paguei**, que te avisa. Ao confirmar, o bot avisa o cliente. Não há confirmação automática: confira o Pix no app do seu banco (o identificador `PED...` do pedido aparece no Pix).

## Pagamento automático (Pix por API)
Na aba **Pagamento**, escolha "Atendimento automático (API de pagamento)" e o intermediador:

| Intermediador | Onde pegar o token | Testes |
|---|---|---|
| **Mercado Pago** (recomendado) | mercadopago.com.br/developers → Suas integrações → Credenciais → Access Token | Token `TEST-...` = testes, `APP_USR-...` = vendas reais |
| **PagBank** | acesso.pagbank.com.br → Venda online → Integrações → Gerar Token | Ambiente Sandbox. Produção exige liberação da API pelo suporte do PagBank |
| **Asaas** | Integrações → Chave de API (`$aact_...`) | Ambiente Sandbox (conta em sandbox.asaas.com) |

1. Cole o token e clique em **Testar token**: ele é conferido no intermediador (e o painel avisa se for token de testes no modo produção ou vice-versa). O token nunca volta para o navegador.
2. Informe seu ID do Telegram para receber o aviso de cada venda paga (mande /start para o seu bot uma vez).
3. A conta no intermediador precisa ter uma chave Pix cadastrada.

A aba **🧪 Testar pagamento** gera um Pix de R$ 1,00 com a configuração salva, mostra o QR Code no próprio painel e confere sozinha se ele foi pago — sem passar pelo Telegram.

## Botões do menu
Na aba **Botões** você monta o menu que fica embaixo do campo de mensagem no Telegram. Cada botão tem uma função pronta: abrir catálogo, ver carrinho, finalizar compra, esvaziar carrinho, enviar mensagem, abrir link, falar com o vendedor, pedir contato, pedir localização ou executar um comando da aba Comandos.
Escolha quantos botões por linha (1 a 3) e veja a pré-visualização. Sem botões cadastrados, o bot usa o menu padrão (Produtos, Carrinho, Finalizar compra). Os clientes veem o menu novo depois de enviar /start de novo.

## Chat limpo
Em **Permissões e chat**, a opção "Chat limpo" (ligada por padrão) faz o bot apagar as mensagens já usadas depois de cada escolha do cliente: catálogo anterior, carrinho, perguntas respondidas e as respostas com nome, CPF e e-mail. O QR Code e o copia e cola somem quando o pagamento é confirmado. O Telegram só permite apagar mensagens com menos de 48 horas.

Bancos tradicionais (Itaú, Bradesco, BB, Santander...) não estão na lista porque a API Pix deles exige contrato de empresa e certificado digital, não só um token.

No bot, o cliente informa nome, CPF e e-mail na primeira compra, recebe o QR Code e o Pix copia e cola com o valor exato, e o pagamento é confirmado sozinho (verificação a cada 30s; os pedidos ficam na tabela `pix_orders`).

Dados ficam em `data/bots.json` (contém os tokens: não compartilhe essa pasta).
O painel escuta só em 127.0.0.1. Não exponha na rede sem proteger com senha.
