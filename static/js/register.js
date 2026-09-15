/* Chai & Trails: live checks on the registration form's username and email.

   These only save the member a round trip. Anyone can switch JavaScript off
   or post to the server directly, so the server repeats every rule on
   submit, and adds the ones that need the database (is the username or
   email already taken?). Nothing here is relied on for security. */
(function () {
  'use strict';

  var form = document.querySelector('[data-live-validate]');
  if (!form) { return; }

  var PAUSE_MS = 400;        // wait for a pause in typing before showing a problem
  var USERNAME_MAX = 150;    // forum_user.username is varchar(150)
  var EMAIL_MAX = 254;       // forum_user.email is varchar(254)

  /* One character of a username. The same rule as Django's
     UnicodeUsernameValidator: letters and digits in any script, and @ . + - _
     Everything else is refused, including the characters HTML or SQL
     injection needs: < > " ' ` ; ( ) = and spaces. */
  var USERNAME_CHAR = /^[\p{L}\p{N}_.@+-]$/u;
  var CODE_CHARS = /[<>"'`;=(){}\\\/&]/;
  var EMAIL_SHAPE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

  function checkUsername(value) {
    if (value === '') { return 'Choose a username.'; }
    var chars = Array.from(value);  // code points, as the database counts them
    var hasSpace = /\s/.test(value);
    var refused = chars.filter(function (ch, i) {
      return !/\s/.test(ch) && !USERNAME_CHAR.test(ch) && chars.indexOf(ch) === i;
    });
    var toRemove = refused.join(' ') + (hasSpace ? (refused.length ? ' and the spaces' : 'the spaces') : '');

    // Named first, so an injection attempt such as  x' OR '1'='1  is called what it is.
    if (CODE_CHARS.test(value)) {
      return 'Usernames can’t contain code or markup characters. Remove ' + toRemove +
        ' (letters, digits and @ . + - _ only).';
    }
    if (hasSpace && !refused.length) { return 'No spaces, please. Your display name can have them.'; }
    if (refused.length) {
      return 'That character isn’t allowed in usernames. Remove ' + toRemove + ' (letters, digits and @ . + - _ only).';
    }
    if (chars.length > USERNAME_MAX) {
      return 'Keep it to ' + USERNAME_MAX + ' characters or fewer (this one has ' + chars.length + ').';
    }
    return '';
  }

  function checkEmail(value) {
    if (value === '') { return 'Enter your email address.'; }
    if (value.length > EMAIL_MAX) { return 'Email addresses can be at most ' + EMAIL_MAX + ' characters.'; }
    if (!EMAIL_SHAPE.test(value)) { return 'That doesn’t look like an email address, such as name@example.com.'; }
    return '';
  }

  var checks = { username: checkUsername, email: checkEmail };

  /* The <ul class="errors"> under a field for live messages, made on first use. */
  function messageList(input) {
    var id = input.id + '_live';
    var list = document.getElementById(id);
    if (!list) {
      list = document.createElement('ul');
      list.className = 'errors';
      list.id = id;
      list.setAttribute('aria-live', 'polite');  // screen readers announce new messages
      input.insertAdjacentElement('afterend', list);
      var describedBy = input.getAttribute('aria-describedby');
      input.setAttribute('aria-describedby', describedBy ? describedBy + ' ' + id : id);
    }
    return list;
  }

  function show(input, message) {
    var list = messageList(input);
    list.textContent = '';
    if (message) {
      var item = document.createElement('li');
      item.textContent = message;  // textContent, never innerHTML: the message repeats what was typed
      list.appendChild(item);
      input.setAttribute('aria-invalid', 'true');
    } else {
      input.removeAttribute('aria-invalid');
    }
  }

  /* Checks the field, shows the result, and returns true when it passes.
     Leading and trailing spaces are ignored, as the server strips them too. */
  function run(input) {
    var message = checks[input.name](input.value.trim());
    show(input, message);
    return message === '';
  }

  Object.keys(checks).forEach(function (name) {
    var input = form.elements[name];
    if (!input) { return; }
    var timer = null;
    var touched = false;

    input.addEventListener('input', function () {
      touched = true;
      clearTimeout(timer);
      // Errors from the last submit described the old value; the live check replaces them.
      Array.prototype.forEach.call(input.parentNode.querySelectorAll('ul.errors'), function (list) {
        if (list.id !== input.id + '_live') { list.remove(); }
      });
      if (checks[name](input.value.trim()) === '') {
        show(input, '');  // good news straight away
      } else {
        timer = setTimeout(function () { run(input); }, PAUSE_MS);
      }
    });

    // Leaving the field shows any problem at once, but not for a field never typed in.
    input.addEventListener('blur', function () {
      if (touched) {
        clearTimeout(timer);
        run(input);
      }
    });
  });

  form.addEventListener('submit', function (event) {
    var firstProblem = null;
    Object.keys(checks).forEach(function (name) {
      var input = form.elements[name];
      if (input && !run(input) && !firstProblem) { firstProblem = input; }
    });
    if (firstProblem) {
      event.preventDefault();
      firstProblem.focus();
    }
  });
})();
