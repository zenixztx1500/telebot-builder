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

## Pagamento automático (Pix via PagBank)
Na aba **Pagamento**, escolha "Atendimento automático (API bancária)":
1. Escolha o ambiente: **Sandbox** (testes) ou **Produção** (vendas reais).
2. Cole o token do PagBank (acesso.pagbank.com.br → Venda online → Integrações → Gerar Token). Ele é conferido ao salvar e nunca volta para o navegador.
3. Informe seu ID do Telegram para receber o aviso de cada venda paga (mande /start para o seu bot uma vez).

No bot, o cliente informa nome, CPF e e-mail na primeira compra, recebe o QR Code e o Pix copia e cola com o valor exato, e o pagamento é confirmado sozinho (verificação a cada 30s; os pedidos ficam na tabela `pix_orders`).

Dados ficam em `data/bots.json` (contém os tokens: não compartilhe essa pasta).
O painel escuta só em 127.0.0.1. Não exponha na rede sem proteger com senha.
