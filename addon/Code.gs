// ============================================================
// Eagle Agent — Gmail Workspace Add-on
// ============================================================
// Provides contextual email information and actions in the
// Gmail sidebar. Communicates with the EagleAgent FastAPI backend
// authenticated via OIDC identity tokens.
// ============================================================

// The backend URL is the base of the EagleAgent deployment.
// In a future version this could be fetched from script properties.
const BACKEND_URL = 'https://eagle-agent.mooball.net';

// ============================================================
// Helper: Authenticated fetch to backend
// ============================================================

/**
 * POST JSON to the addon API endpoint with an OIDC identity token.
 *
 * ScriptApp.getIdentityToken() requires that:
 *   1. The Apps Script project is linked to a GCP project (done).
 *   2. The 'openid' scope is in appsscript.json (done).
 *   3. The OAuth consent screen has openid scope (done).
 *
 * The token is an OIDC JWT with claims: iss, aud, sub, hd, email,
 * email_verified, name, iat, exp.
 *
 * The backend verifies: signature, issuer (accounts.google.com),
 * expiry, and hd == "eagle-exports.com.au".
 */
function fetchBackend(path, payload) {
  var idToken = ScriptApp.getIdentityToken();
  if (!idToken) {
    throw new Error(
      'Unable to get identity token. Ensure openid scope is granted ' +
      'and the script is linked to a GCP project.'
    );
  }

  var options = {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + idToken
    },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  };

  var response = UrlFetchApp.fetch(BACKEND_URL + path, options);
  var code = response.getResponseCode();

  if (code === 401) {
    throw new Error('Authentication failed. Contact admin.');
  }
  if (code === 403) {
    throw new Error('Access denied — only eagle-exports.com.au users.');
  }
  if (code >= 400) {
    throw new Error(
      'Backend error (' + code + '): ' + response.getContentText()
    );
  }

  return JSON.parse(response.getContentText());
}

// ============================================================
// Trigger: Homepage (no message context)
// ============================================================
// Called when the user opens the add-on from the Gmail sidebar
// without having a specific message selected.

function onHomepage(e) {
  var card = CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader()
        .setTitle('Eagle Agent')
        .setSubtitle('Open an email to see context')
    )
    .addSection(
      CardService.newCardSection()
        .addWidget(
          CardService.newTextParagraph()
            .setText(
              'Select an email to view linked customers, ' +
              'suppliers, RFQs, and opportunities.'
            )
        )
    )
    .build();

  return [card];
}

// ============================================================
// Trigger: Message opened — fetch context from backend
// ============================================================
// Called when the user opens a Gmail message while the add-on
// is open. The event object provides the message ID and an
// access token for reading message metadata.

function onGmailMessageOpen(e) {
  // Activate temporary Gmail scopes for reading message metadata
  var accessToken = e.gmail.accessToken;
  GmailApp.setCurrentMessageAccessToken(accessToken);

  var messageId = e.gmail.messageId;
  var message = GmailApp.getMessageById(messageId);
  var subject = message.getSubject();
  var sender = message.getFrom();
  var threadId = message.getThread().getId();

  // Call backend for entity linking context
  var context;
  try {
    context = fetchBackend('/api/addon/context', {
      gmail_message_id: messageId,
      gmail_thread_id: threadId,
      subject: subject,
      sender: sender
    });
  } catch (err) {
    return [buildErrorCard(err.message)];
  }

  return [buildContextCard(context, messageId, threadId, subject, sender)];
}

// ============================================================
// UI: Build the context card
// ============================================================

function buildContextCard(context, messageId, threadId, subject, sender) {
  var builder = CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader()
        .setTitle('Eagle Agent')
        .setSubtitle(subject.length > 60 ? subject.substring(0, 57) + '...' : subject)
    );

  // ---- Status section: linked entities ----
  var statusSection = CardService.newCardSection().setHeader('Linked Entities');

  if (context.customer) {
    statusSection.addWidget(
      CardService.newDecoratedText()
        .setTopLabel('Customer')
        .setText(context.customer.name)
        .setStartIcon(
          CardService.newIconImage().setIcon(CardService.Icon.PERSON)
        )
    );
  }

  if (context.supplier) {
    statusSection.addWidget(
      CardService.newDecoratedText()
        .setTopLabel('Supplier')
        .setText(context.supplier.name)
        .setStartIcon(
          CardService.newIconImage().setIcon(CardService.Icon.STAR)
        )
    );
  }

  if (context.rfq) {
    statusSection.addWidget(
      CardService.newDecoratedText()
        .setTopLabel('RFQ')
        .setText(context.rfq.rfq_number + ' \u2014 ' + context.rfq.status)
        .setStartIcon(
          CardService.newIconImage().setIcon(CardService.Icon.DESCRIPTION)
        )
    );
  }

  if (context.opportunity) {
    statusSection.addWidget(
      CardService.newDecoratedText()
        .setTopLabel('Opportunity')
        .setText(context.opportunity.title)
        .setStartIcon(
          CardService.newIconImage().setIcon(CardService.Icon.BOOKMARK)
        )
    );
  }

  if (
    !context.customer &&
    !context.supplier &&
    !context.rfq &&
    !context.opportunity
  ) {
    statusSection.addWidget(
      CardService.newTextParagraph()
        .setText('<i>No linked entities found for this email.</i>')
    );
  }

  builder.addSection(statusSection);

  // ---- Actions section (Phase 2 — placeholder buttons) ----
  var actionsSection = CardService.newCardSection().setHeader('Actions');

  actionsSection.addWidget(
    CardService.newTextButton()
      .setText('Link to Customer/Supplier')
      .setOnClickAction(
        CardService.newAction()
          .setFunctionName('onLinkEntity')
          .setParameters({
            messageId: messageId,
            threadId: threadId,
            subject: subject,
            sender: sender
          })
      )
  );

  actionsSection.addWidget(
    CardService.newTextButton()
      .setText('Link to RFQ')
      .setOnClickAction(
        CardService.newAction()
          .setFunctionName('onLinkRfq')
          .setParameters({
            messageId: messageId,
            threadId: threadId,
            subject: subject
          })
      )
  );

  actionsSection.addWidget(
    CardService.newTextButton()
      .setText('Create New RFQ')
      .setOnClickAction(
        CardService.newAction()
          .setFunctionName('onCreateRfq')
          .setParameters({
            messageId: messageId,
            threadId: threadId,
            subject: subject,
            sender: sender
          })
      )
  );

  builder.addSection(actionsSection);

  return builder.build();
}

// ============================================================
// UI: Error card
// ============================================================

function buildErrorCard(message) {
  return CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader().setTitle('Eagle Agent')
    )
    .addSection(
      CardService.newCardSection()
        .addWidget(
          CardService.newDecoratedText()
            .setTopLabel('Error')
            .setText(message)
            .setStartIcon(
              CardService.newIconImage().setIcon(CardService.Icon.INVITE)
            )
        )
    )
    .build();
}

// ============================================================
// Action stubs (to be implemented in Phase 2)
// ============================================================

function onLinkEntity(e) {
  return CardService.newActionResponseBuilder()
    .setNotification(
      CardService.newNotification()
        .setText('Coming soon \u2014 Link to Customer/Supplier')
    )
    .build();
}

function onLinkRfq(e) {
  return CardService.newActionResponseBuilder()
    .setNotification(
      CardService.newNotification()
        .setText('Coming soon \u2014 Link to RFQ')
    )
    .build();
}

function onCreateRfq(e) {
  return CardService.newActionResponseBuilder()
    .setNotification(
      CardService.newNotification()
        .setText('Coming soon \u2014 Create RFQ')
    )
    .build();
}
