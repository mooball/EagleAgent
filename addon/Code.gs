// ============================================================
// Eagle Agent — Gmail Workspace Add-on
// ============================================================
// Provides contextual email information and actions in the
// Gmail sidebar. Communicates with the EagleAgent FastAPI backend
// authenticated via OIDC identity tokens.
// ============================================================

// The backend URL is the base of the EagleAgent deployment.
// In a future version this could be fetched from script properties.
const BACKEND_URL = 'https://agent.eaglexp.com.au';

// ============================================================
// Domain blacklist — emails from these domains show a simplified
// card with no link actions to avoid false matches.
// ============================================================
var DOMAIN_BLACKLIST = [
  'google.com',
  'accounts.google.com',
  'eagle-exports.com.au',
  'eaglexp.com',
  'eaglexp.com.au',
  'eagle-exports.com'
];

/**
 * Extract the domain from a Gmail sender string.
 * Handles formats: "Name" <email@domain.com> or plain email@domain.com
 */
function extractDomain(sender) {
  var match = sender.match(/<([^>]+)>/) || [null, sender];
  var addr = match[1].trim().toLowerCase();
  var parts = addr.split('@');
  return parts.length === 2 ? parts[1] : '';
}

/**
 * Check if the sender's domain is in the blacklist.
 */
function isBlacklisted(sender) {
  var domain = extractDomain(sender);
  if (!domain) return false;
  for (var i = 0; i < DOMAIN_BLACKLIST.length; i++) {
    if (domain === DOMAIN_BLACKLIST[i]) return true;
  }
  return false;
}

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
    throw new Error('Access denied — only eagle-exports.com users.');
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
  // Guard: if running from editor (no event), return a fallback card
  if (!e || !e.gmail) {
    return [buildEditorFallbackCard()];
  }

  // Activate temporary Gmail scopes for reading message metadata
  var accessToken = e.gmail.accessToken;
  GmailApp.setCurrentMessageAccessToken(accessToken);

  var messageId = e.gmail.messageId;
  var message = GmailApp.getMessageById(messageId);
  var subject = message.getSubject();
  var sender = message.getFrom();
  var threadId = message.getThread().getId();

  // Skip backend call for blacklisted domains (Google services, internal)
  if (isBlacklisted(sender)) {
    return [buildNoActionsCard(subject, sender)];
  }

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

  // ---- Actions section ----
  var actionsSection = CardService.newCardSection().setHeader('Actions');

  // Only show "Link to Customer/Supplier" if neither is already linked
  if (!context.customer && !context.supplier) {
    actionsSection.addWidget(
      CardService.newTextButton()
        .setText('Link to Customer / Supplier')
        .setOnClickAction(
          CardService.newAction()
            .setFunctionName('onLinkEntity')
            .setParameters({
              messageId: messageId,
              threadId: threadId,
              sender: sender
            })
        )
    );
  }

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
// UI: Fallback card for editor / no-Gmail-context
// ============================================================

function buildEditorFallbackCard() {
  return CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader().setTitle('Eagle Agent')
    )
    .addSection(
      CardService.newCardSection()
        .addWidget(
          CardService.newTextParagraph()
            .setText(
              'This add-on works inside Gmail. <b>Open an email</b> ' +
              'in Gmail to see linked customers, suppliers, RFQs, ' +
              'and opportunities.'
            )
        )
    )
    .build();
}

// ============================================================
// UI: No-actions card (for blacklisted / service domains)
// ============================================================

function buildNoActionsCard(subject, sender) {
  var domain = extractDomain(sender);
  return CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader()
        .setTitle('Eagle Agent')
        .setSubtitle(
          subject.length > 60 ? subject.substring(0, 57) + '...' : subject
        )
    )
    .addSection(
      CardService.newCardSection()
        .addWidget(
          CardService.newTextParagraph()
            .setText(
              '<i>This email is from <b>' + domain + '</b> — a service ' +
              'or internal domain. No linking actions are needed.</i>'
            )
        )
    )
    .build();
}

// ============================================================
// Phase 2: Link to Customer / Supplier — multi-step flow
// ============================================================

/**
 * Step 1: User clicks "Link to Customer / Supplier" on the context card.
 * Push a card asking them to choose Customer or Supplier.
 */
function onLinkEntity(e) {
  var card = CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader()
        .setTitle('Link to Customer or Supplier?')
        .setSubtitle('Choose the entity type')
    )
    .addSection(
      CardService.newCardSection()
        .addWidget(
          CardService.newTextButton()
            .setText('Customer')
            .setOnClickAction(
              CardService.newAction()
                .setFunctionName('onChooseEntityType')
                .setParameters({
                  messageId: e.parameters.messageId,
                  threadId: e.parameters.threadId,
                  sender: e.parameters.sender,
                  linkType: 'customer'
                })
            )
        )
        .addWidget(
          CardService.newTextButton()
            .setText('Supplier')
            .setOnClickAction(
              CardService.newAction()
                .setFunctionName('onChooseEntityType')
                .setParameters({
                  messageId: e.parameters.messageId,
                  threadId: e.parameters.threadId,
                  sender: e.parameters.sender,
                  linkType: 'supplier'
                })
            )
        )
    )
    .build();

  var nav = CardService.newNavigation().pushCard(card);
  return CardService.newActionResponseBuilder()
    .setNavigation(nav)
    .build();
}

/**
 * Step 2: User chose Customer or Supplier. Push a search card with a text input.
 */
function onChooseEntityType(e) {
  var linkType = e.parameters.linkType;
  var label = linkType === 'customer' ? 'Customer' : 'Supplier';

  var searchInput = CardService.newTextInput()
    .setFieldName('searchQuery')
    .setTitle('Search ' + label + 's')
    .setHint('Type at least 2 characters...')
    .setOnChangeAction(
      CardService.newAction()
        .setFunctionName('onSearchEntity')
        .setParameters({
          messageId: e.parameters.messageId,
          threadId: e.parameters.threadId,
          linkType: linkType
        })
    );

  var card = CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader()
        .setTitle('Link to ' + label)
    )
    .addSection(
      CardService.newCardSection()
        .addWidget(searchInput)
    )
    .addSection(
      CardService.newCardSection()
        .setHeader('Results')
        .addWidget(
          CardService.newTextParagraph()
            .setText('<i>Type to search...</i>')
        )
    )
    .build();

  var nav = CardService.newNavigation().pushCard(card);
  return CardService.newActionResponseBuilder()
    .setNavigation(nav)
    .build();
}

/**
 * Step 3: User typed in the search box. Call the backend and rebuild the
 * results section of the current card.
 */
function onSearchEntity(e) {
  var query = e.formInput.searchQuery || '';
  var linkType = e.parameters.linkType;

  if (query.length < 2) {
    // Not enough characters — show prompt
    var emptyCard = CardService.newCardBuilder()
      .setHeader(
        CardService.newCardHeader().setTitle(
          'Link to ' + (linkType === 'customer' ? 'Customer' : 'Supplier')
        )
      )
      .addSection(
        CardService.newCardSection()
          .addWidget(
            CardService.newTextInput()
              .setFieldName('searchQuery')
              .setTitle(
                'Search ' +
                (linkType === 'customer' ? 'Customer' : 'Supplier') +
                's'
              )
              .setHint('Type at least 2 characters...')
              .setOnChangeAction(
                CardService.newAction()
                  .setFunctionName('onSearchEntity')
                  .setParameters({
                    messageId: e.parameters.messageId,
                    threadId: e.parameters.threadId,
                    linkType: linkType
                  })
              )
          )
      )
      .addSection(
        CardService.newCardSection()
          .setHeader('Results')
          .addWidget(
            CardService.newTextParagraph()
              .setText('<i>Type at least 2 characters to search...</i>')
          )
      )
      .build();

    var nav = CardService.newNavigation().updateCard(emptyCard);
    return CardService.newActionResponseBuilder()
      .setNavigation(nav)
      .build();
  }

  // Call backend search
  var url = BACKEND_URL + '/api/addon/search?type=' +
            encodeURIComponent(linkType) + '&q=' + encodeURIComponent(query);
  var idToken = ScriptApp.getIdentityToken();

  try {
    var response = UrlFetchApp.fetch(url, {
      method: 'get',
      headers: { Authorization: 'Bearer ' + idToken },
      muteHttpExceptions: true
    });

    if (response.getResponseCode() !== 200) {
      throw new Error('Search failed: ' + response.getContentText());
    }

    var data = JSON.parse(response.getContentText());
    var results = data.results || [];

    // Build results card
    var label = linkType === 'customer' ? 'Customer' : 'Supplier';
    var resultsCard = CardService.newCardBuilder()
      .setHeader(
        CardService.newCardHeader().setTitle('Link to ' + label)
      );

    var inputSection = CardService.newCardSection()
      .addWidget(
        CardService.newTextInput()
          .setFieldName('searchQuery')
          .setTitle('Search ' + label + 's')
          .setValue(query)
          .setOnChangeAction(
            CardService.newAction()
              .setFunctionName('onSearchEntity')
              .setParameters({
                messageId: e.parameters.messageId,
                threadId: e.parameters.threadId,
                linkType: linkType
              })
          )
      );
    resultsCard.addSection(inputSection);

    var resultSection = CardService.newCardSection().setHeader('Results');

    if (results.length === 0) {
      resultSection.addWidget(
        CardService.newTextParagraph()
          .setText('<i>No ' + label + 's found matching "' + query + '".</i>')
      );
    } else {
      for (var i = 0; i < results.length; i++) {
        var entity = results[i];
        resultSection.addWidget(
          CardService.newDecoratedText()
            .setText(entity.name)
            .setWrapText(true)
            .setButton(
              CardService.newTextButton()
                .setText('Link')
                .setOnClickAction(
                  CardService.newAction()
                    .setFunctionName('onSelectEntity')
                    .setParameters({
                      messageId: e.parameters.messageId,
                      threadId: e.parameters.threadId,
                      linkType: linkType,
                      entityId: entity.id,
                      entityName: entity.name
                    })
                )
            )
        );
      }
    }

    resultsCard.addSection(resultSection);

    var nav = CardService.newNavigation().updateCard(resultsCard.build());
    return CardService.newActionResponseBuilder()
      .setNavigation(nav)
      .build();

  } catch (err) {
    var errCard = CardService.newCardBuilder()
      .setHeader(CardService.newCardHeader().setTitle('Search Error'))
      .addSection(
        CardService.newCardSection()
          .addWidget(
            CardService.newTextParagraph()
              .setText('<font color="#d93025">' + err.message + '</font>')
          )
      )
      .build();

    var nav = CardService.newNavigation().updateCard(errCard);
    return CardService.newActionResponseBuilder()
      .setNavigation(nav)
      .build();
  }
}

/**
 * Step 4: User selected an entity. Call backend to link, show success, pop back.
 */
function onSelectEntity(e) {
  var result;
  try {
    result = fetchBackend('/api/addon/link-email', {
      gmail_message_id: e.parameters.messageId,
      gmail_thread_id: e.parameters.threadId,
      link_type: e.parameters.linkType,
      entity_id: e.parameters.entityId
    });
  } catch (err) {
    return CardService.newActionResponseBuilder()
      .setNotification(
        CardService.newNotification().setText('Error: ' + err.message)
      )
      .build();
  }

  // Pop back to the context card, which will now show the linked entity
  var nav = CardService.newNavigation().popToRoot();
  return CardService.newActionResponseBuilder()
    .setNavigation(nav)
    .setNotification(
      CardService.newNotification().setText(
        'Linked to ' + result.entity_name
      )
    )
    .build();
}
